# Copyright (C) 2021, QuantStack
# SPDX-License-Identifier: BSD-3-Clause

from boa.core.solver import get_solver
import copy
import json
from pathlib import Path
import sys

import rich
from rich.table import Table
from rich.padding import Padding

from conda.base.context import context
from conda_build.config import get_or_merge_config
from conda_build.utils import apply_pin_expressions, RUN_EXPORTS_TYPES
from conda.models.channel import Channel as CondaChannel
from conda_build.metadata import eval_selector, ns_cfg
from conda_build.jinja_context import native_compiler

from libmambapy import Context as MambaContext
from boa.core.config import boa_config
from boa.core.conda_build_spec import CondaBuildSpec
from boa.helpers.ast_extract_syms import ast_extract_syms

console = boa_config.console


class Output:
    def __init__(
        self, d, config, parent=None, conda_build_config=None, selected_features=None
    ):
        if parent is None:
            parent = {}

        self.selected_features = selected_features or {}
        self.data = d
        self.data["source"] = d.get("source", parent.get("source", {}))
        self.config = config
        self.conda_build_config = conda_build_config or {}
        self.name = d["step"]["name"]
        if "package" in d:
            self.version = d["package"]["version"]
            self.build_string = d["package"].get("build_string")
        else:
            self.version = None
            self.build_string = None

        self.build_number = d["build"].get("number", 0)
        self.noarch = d["build"].get("noarch", False)
        self.is_first = False
        self.is_package = "package" in d

        self.sections = {}

        def set_section(sname):
            self.sections[sname] = {}
            self.sections[sname].update(parent.get(sname, {}))
            self.sections[sname].update(d.get(sname, {}))

        set_section("build")
        set_section("package")
        set_section("app")
        set_section("extra")
        set_section("test")

        self.sections["files"] = d.get("files")
        self.sections["source"] = self.data.get("source", {})
        if hasattr(self.sections["source"], "keys"):
            self.sections["source"] = [self.sections["source"]]

        self.required_steps = []
        for s in self.sections["source"]:
            if "step" in s:
                self.required_steps += [s["step"]]

        self.sections["features"] = parent.get("features", [])

        self.feature_map = {f["name"]: f for f in self.sections.get("features", [])}
        for fname, feat in self.feature_map.items():
            activated = feat.get("default", False)
            if fname in self.selected_features:
                activated = self.selected_features[fname]

            feat["activated"] = activated

        if self.feature_map.get("static") and self.feature_map["static"]["activated"]:
            self.name += "-static"

        if len(self.feature_map):
            table = Table()
            table.title = "Activated Features"
            table.add_column("Feature")
            table.add_column("State")
            for feature in self.feature_map:
                if self.feature_map[feature]["activated"]:
                    table.add_row(feature, "[green]ON[/green]")
                else:
                    table.add_row(feature, "[red]OFF[/red]")

            console.print(table)

        self.requirements = copy.copy(d.get("requirements", {}))
        for f in self.feature_map.values():
            if f["activated"]:
                if not f.get("requirements"):
                    continue
                for i in ["build", "host", "run", "run_constrained"]:
                    base_req = self.requirements.get(i, [])
                    feat_req = f["requirements"].get(i, [])
                    base_req += feat_req
                    if len(base_req):
                        self.requirements[i] = base_req

        self.transactions = {}

        self.parent = parent

        for section in ("build", "host", "run", "run_constrained"):
            self.requirements[section] = [
                CondaBuildSpec(r) for r in (self.requirements.get(section) or [])
            ]
        self.sections["requirements"] = self.requirements

        # handle strong and weak run exports
        self.run_exports = {key: [] for key in RUN_EXPORTS_TYPES}
        if self.sections["build"].get("run_exports"):
            if isinstance(self.sections["build"]["run_exports"], list):
                self.run_exports["weak"] = [
                    CondaBuildSpec(el) for el in self.sections["build"]["run_exports"]
                ]
            else:
                for strength in RUN_EXPORTS_TYPES:
                    self.run_exports[strength] = [
                        CondaBuildSpec(el)
                        for el in self.sections["build"]["run_exports"].get(
                            strength, []
                        )
                    ]

    def skip(self):
        skips = self.sections["build"].get("skip", [])
        skip_reasons = []
        for x in skips:
            if eval_selector(x, ns_cfg(self.config), []):
                skip_reasons.append(x)
        if len(skip_reasons):
            console.print(
                f"[green]Skipping {self.name} {' | '.join(self.differentiating_variant)} because of[/green]\n"
                + "\n".join([f"  - {x}" for x in skip_reasons])
            )
        return len(skip_reasons) != 0

    def inherit_requirements(self, steps):
        def merge_requirements(a, b):
            b_names = [x.name for x in b]
            for r in a:
                print(r)
                if r.name in b_names:
                    continue
                else:
                    print(r, "is inherited!!!")

                    rc = copy.deepcopy(r)
                    rc.is_inherited = True
                    b.append(rc)

        for s in self.required_steps:
            merge_requirements(
                steps[s].requirements["build"], self.requirements["build"]
            )
            merge_requirements(steps[s].requirements["host"], self.requirements["host"])

    def variant_keys(self):
        all_keys = self.requirements.get("build", []) + self.requirements.get(
            "host", []
        )

        for s in self.sections["build"].get("skip", []):
            all_keys += ast_extract_syms(s)

        return [str(x) for x in all_keys]

    def all_requirements(self):
        requirements = (
            self.requirements.get("build")
            + self.requirements.get("host")
            + self.requirements.get("run")
            + self.run_exports.get("weak")
            + self.run_exports.get("strong")
            + self.run_exports.get("noarch")
        )

        return requirements

    def apply_variant(self, variant, differentiating_keys=()):
        copied = copy.deepcopy(self)

        copied.variant = variant
        for idx, r in enumerate(self.requirements["build"]):
            vname = r.name.replace("-", "_")
            if vname in variant:
                copied.requirements["build"][idx] = CondaBuildSpec(
                    r.name + " " + variant[vname]
                )
                copied.requirements["build"][idx].from_pinnings = True
                copied.requirements["build"][idx].is_inherited = r.is_inherited

        for idx, r in enumerate(self.requirements["host"]):
            vname = r.name.replace("-", "_")
            if vname in variant:
                copied.requirements["host"][idx] = CondaBuildSpec(
                    r.name + " " + variant[vname]
                )
                copied.requirements["host"][idx].from_pinnings = True
                copied.requirements["host"][idx].is_inherited = r.is_inherited

        # todo figure out if we should pin like that in the run reqs as well?
        # for idx, r in enumerate(self.requirements["run"]):
        #     vname = r.name.replace("-", "_")
        #     if vname in variant:
        #         copied.requirements["run"][idx] = CondaBuildSpec(
        #             r.name + " " + variant[vname]
        #         )
        #         copied.requirements["run"][idx].from_pinnings = True

        # insert compiler_cxx, compiler_c and compiler_fortran
        for idx, r in enumerate(self.requirements["build"]):
            if r.name.startswith("COMPILER_"):
                lang = r.splitted[1].lower()
                if variant.get(lang + "_compiler"):
                    compiler = (
                        f"{variant[lang + '_compiler']}_{variant['target_platform']}"
                    )
                else:
                    compiler = f"{native_compiler(lang, copied.config)}_{variant['target_platform']}"
                if variant.get(lang + "_compiler_version"):
                    version = variant[lang + "_compiler_version"]
                    copied.requirements["build"][idx].final = f"{compiler} {version}*"
                else:
                    copied.requirements["build"][idx].final = f"{compiler}"
                copied.requirements["build"][idx].from_pinnings = True

        for r in self.requirements["host"]:
            if r.name.startswith("COMPILER_"):
                raise RuntimeError("Compiler should be in build section")

        copied.config = get_or_merge_config(self.config, variant=variant)

        copied.differentiating_keys = differentiating_keys
        copied.differentiating_variant = []
        for k in differentiating_keys:
            copied.differentiating_variant.append(variant[k])

        return copied

    def to_json(self):
        res = {
            "name": self.name,
            "version": self.version,
            "build_number": self.build_number,
            "source": self.data["source"],
            "noarch": self.noarch,
        }

        res["differentiating_variant"] = self.differentiating_variant
        res["variant"] = self.variant

        def specs_to_dict(specs):
            ret = []
            for s in specs:
                attrs = []
                if s.is_pin:
                    attrs.append("is_pin")
                if s.from_run_export:
                    attrs.append("from_run_export")
                if s.from_pinnings:
                    attrs.append("from_pinnings")

                sdict = {
                    "spec": s.raw,
                    "name": s.final_name,
                    "attrs": attrs,
                    "resolved": {},
                }
                if hasattr(s, "final_version"):
                    sdict["resolved"]["final_version"] = s.final_version

                ret.append(sdict)
            return ret

        res["requirements"] = {
            "build": specs_to_dict(self.requirements["build"]),
            "host": specs_to_dict(self.requirements["host"]),
            "run": specs_to_dict(self.requirements["run"]),
        }

        return res

    def __rich__(self):
        table = Table(box=rich.box.MINIMAL_DOUBLE_HEAD)
        s = f"Output: {self.name} {self.version} BN: {self.build_number}\n"
        if hasattr(self, "differentiating_variant"):
            short_v = " ".join([val for val in self.differentiating_variant])
            s += f"Variant: {short_v}\n"
        s += "Build:\n"
        table.title = s
        table.add_column("Dependency")
        table.add_column("Version requirement")
        table.add_column("Selected")
        table.add_column("Build")
        table.add_column("Channel")

        def spec_format(x):
            version, fv = " ", " "
            channel = CondaChannel.from_url(x.channel).name

            if (
                x.channel.startswith("file://")
                and context.local_build_root in x.channel
            ):
                channel = "local"

            if len(x.final.split(" ")) > 1:
                version = " ".join(r.final.split(" ")[1:])
            if hasattr(x, "final_version"):
                fv = x.final_version
            color = "white"
            if x.from_run_export:
                color = "blue"
            if x.from_pinnings:
                color = "green"
            if x.is_transitive_dependency:
                table.add_row(
                    f"{r.final_name}", "", f"{fv[0]}", f"{fv[1]}", f"{channel}"
                )
                return

            if x.is_pin:
                if x.is_pin_compatible:
                    version = "PC " + version
                else:
                    version = "PS " + version
                color = "cyan"

            name = r.final_name
            if x.is_inherited:
                name += " (inherited)"
                color = "magenta"

            if len(fv) >= 2:
                table.add_row(
                    f"[bold white]{name}[/bold white]",
                    f"[{color}]{version}[/{color}]",
                    f"{fv[0]}",
                    f"{fv[1]}",
                    f"{channel}",
                )
            else:
                table.add_row(
                    f"[bold white]{name}[/bold white]",
                    f"[{color}]{version}[/{color}]",
                    f"{fv[0]}",
                    "",
                    f"{channel}",
                )

        def add_header(header, head=False):
            p = Padding("", (0, 0), style="black")
            if head:
                pns = Padding("", (0, 0), style="black")
                table.add_row(pns, pns, pns, pns, pns)
            table.add_row(Padding(header, (0, 0), style="bold yellow"), p, p, p, p)

        if self.requirements["build"]:
            add_header("Build")
            for r in self.requirements["build"]:
                spec_format(r)
        if self.requirements["host"]:
            add_header("Host", True)
            for r in self.requirements["host"]:
                spec_format(r)
        if self.requirements["run"]:
            add_header("Run", True)
            for r in self.requirements["run"]:
                spec_format(r)
        if self.requirements["run_constrained"]:
            add_header("Run Constraints", True)
            for r in self.requirements["run_constrained"]:
                spec_format(r)
        return table

    def __repr__(self):
        s = f"Output: {self.name} {self.version} BN: {self.build_number}\n"
        if hasattr(self, "differentiating_variant"):
            short_v = " ".join([val for val in self.differentiating_variant])
            s += f"Variant: {short_v}\n"
        s += "Build:\n"

        def spec_format(x):
            version, fv = " ", " "
            if len(x.final.split(" ")) > 1:
                version = " ".join(r.final.split(" ")[1:])
            if hasattr(x, "final_version"):
                fv = x.final_version
            color = "white"
            if x.from_run_export:
                color = "blue"
            if x.from_pinnings:
                color = "green"
            if x.is_transitive_dependency:
                return f" - {r.final_name:<51} {fv[0]:<10} {fv[1]:<10}\n"
            if x.is_pin:
                if x.is_pin_compatible:
                    version = "PC " + version
                else:
                    version = "PS " + version
                color = "cyan"

            channel = CondaChannel.from_url(x.channel).name

            if len(fv) >= 2:
                return f" - [white]{r.final_name:<30}[/white] [{color}]{version:<20}[/{color}] {fv[0]:<10} {fv[1]:<20} {channel}\n"
            else:
                return f" - [white]{r.final_name:<30}[/white] [{color}]{version:<20}[/{color}] {fv[0]:<20} {channel}\n"

        for r in self.requirements["build"]:
            s += spec_format(r)
        s += "Host:\n"
        for r in self.requirements["host"]:
            s += spec_format(r)
        s += "Run:\n"
        for r in self.requirements["run"]:
            s += spec_format(r)
        return s

    def propagate_run_exports(self, env, pkg_cache):
        # find all run exports
        collected_run_exports = []
        config_pins = self.conda_build_config.get("pin_run_as_build", {})
        for s in self.requirements[env]:
            if s.is_transitive_dependency:
                continue
            if s.name in self.sections["build"].get("ignore_run_exports", []):
                continue

            if hasattr(s, "final_version"):
                final_triplet = s.final_triplet
            else:
                console.print(f"[red]{s} has no final version")
                continue

            if s.name.replace("-", "_") in config_pins:
                s.run_exports_info = {
                    "weak": [
                        f"{s.final_name} {apply_pin_expressions(s.final_version[0], **config_pins[s.name.replace('-', '_')])}"
                    ]
                }
                collected_run_exports.append(s.run_exports_info)
            else:
                path = Path(pkg_cache).joinpath(
                    final_triplet,
                    "info",
                    "run_exports.json",
                )
                if path.exists():
                    with open(path) as fi:
                        run_exports_info = json.load(fi)
                        s.run_exports_info = run_exports_info
                        collected_run_exports.append(run_exports_info)
                else:
                    s.run_exports_info = None

        def append_or_replace(env, spec):
            spec = CondaBuildSpec(spec)
            name = spec.name
            spec.from_run_export = True
            for idx, r in enumerate(self.requirements[env]):
                if r.final_name == name and r.is_simple:
                    self.requirements[env][idx] = spec
                    return
            self.requirements[env].append(spec)

        if env == "build":
            for rex in collected_run_exports:
                if not self.noarch:
                    for r in rex.get("strong", []):
                        append_or_replace("host", r)
                        append_or_replace("run", r)
                    for r in rex.get("weak", []):
                        append_or_replace("host", r)
                    for r in rex.get("strong_constrains", []):
                        append_or_replace("run_constrained", r)

        if env == "host":
            for rex in collected_run_exports:
                if not self.noarch:
                    for r in rex.get("strong", []):
                        append_or_replace("run", r)
                    for r in rex.get("weak", []):
                        append_or_replace("run", r)
                    for r in rex.get("strong_constrains", []):
                        append_or_replace("run_constrained", r)
                    for r in rex.get("weak_constrains", []):
                        append_or_replace("run_constrained", r)
                else:
                    for r in rex.get("noarch", []):
                        append_or_replace("run", r)

    def _solve_env(self, env, all_outputs):
        if self.requirements.get(env):
            console.print(f"Finalizing [yellow]{env}[/yellow] for {self.name}")
            specs = self.requirements[env]

            for s in specs:
                if s.is_pin_subpackage:
                    s.eval_pin_subpackage(all_outputs)
                if env == "run" and s.is_pin_compatible:
                    s.eval_pin_compatible(
                        self.requirements["build"], self.requirements["host"]
                    )

            # save finalized requirements in data for usage in metadata
            self.data["requirements"][env] = [s.final for s in self.requirements[env]]

            spec_map = {s.final_name: s for s in specs}
            specs = [str(x) for x in specs]

            if env in ("host", "run") and not self.config.subdirs_same:
                subdir = self.config.host_subdir
            else:
                subdir = self.config.build_subdir

            solver, pkg_cache = get_solver(
                subdir, output_folder=self.config.output_folder
            )
            if env == "host":
                MambaContext().target_prefix = self.config.host_prefix
                # solver.replace_installed(self.config.host_prefix)
            elif env == "build":
                MambaContext().target_prefix = self.config.build_prefix
                # solver.replace_installed(self.config.build_prefix)
            t = solver.solve(specs, [pkg_cache])

            _, install_pkgs, _ = t.to_conda()
            for _, _, p in install_pkgs:
                p = json.loads(p)
                if p["name"] in spec_map:
                    spec_map[p["name"]].final_version = (
                        p["version"],
                        p["build_string"],
                    )
                    spec_map[p["name"]].channel = p["channel"]
                else:
                    cbs = CondaBuildSpec(f"{p['name']}")
                    cbs.is_transitive_dependency = True
                    cbs.final_version = (p["version"], p["build_string"])
                    cbs.channel = p["channel"]
                    self.requirements[env].append(cbs)

            self.transactions[env] = {
                "transaction": t,
                "pkg_cache": pkg_cache,
            }

            downloaded = t.fetch_extract_packages()
            if not downloaded:
                raise RuntimeError("Did not succeed in downloading packages.")

            if env in ("build", "host"):
                self.propagate_run_exports(env, self.transactions[env]["pkg_cache"])

    def set_final_build_id(self, meta, all_outputs):
        self.final_build_id = meta.build_id()

        final_run_exports = {}
        # we need to evaluate run_exports pin_subpackage here
        for k, run_exports_list in self.run_exports.items():
            for x in run_exports_list:
                if self.name.endswith("-static") and self.name.startswith(x.name):
                    # remove self-run-exports for static packages
                    run_exports_list[:] = []
                else:
                    x.eval_pin_subpackage(all_outputs)

            if run_exports_list:
                final_run_exports[k] = [x.final for x in run_exports_list]

        self.data["build"]["run_exports"] = final_run_exports or None

    def finalize_solve(self, all_outputs):

        self._solve_env("build", all_outputs)
        self._solve_env("host", all_outputs)
        self._solve_env("run", all_outputs)

        # TODO figure out if we can avoid this?!
        if self.config.variant.get("python") is None:
            for r in self.requirements["build"] + self.requirements["host"]:
                if r.name == "python":
                    self.config.variant["python"] = r.final_version[0]

        if self.config.variant.get("python") is None:
            self.config.variant["python"] = ".".join(
                [str(v) for v in sys.version_info[:3]]
            )

        self.variant = self.config.variant
