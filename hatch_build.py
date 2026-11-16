"""Build only reviewed, public application resources into release distributions."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        bundled = root / "src/incident_desk/resources"
        # An sdist already includes the curated tree under src; package discovery
        # will include it once. Forcing it again would create duplicate wheel paths.
        if bundled.is_dir():
            manifest = json.loads((bundled / "manifest.json").read_text())
            for relative, digest in manifest.items():
                path = bundled / relative
                if not path.resolve().is_relative_to(bundled.resolve()):
                    raise RuntimeError("Unsafe path in bundled resource manifest")
                if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise RuntimeError("Bundled resource integrity check failed")
            # The public UI is named dist, which source .gitignore deliberately
            # excludes. Mark the curated tree as an artifact so wheels retain it.
            build_data.setdefault("artifacts", []).append("src/incident_desk/resources/**")
            self._temporary = None
            return
        temporary = Path(tempfile.mkdtemp(prefix="incidentdesk-resources-"))
        resource = temporary / "resources"
        resource.mkdir()
        self._temporary = temporary
        try:
            # Wheels rebuilt from an sdist reuse only its curated resource tree.
            if bundled.is_dir():
                shutil.copytree(bundled, resource, dirs_exist_ok=True)
            else:
                selected = {
                    "frontend/dist": ("*.html", "*.js", "*.css", "*.svg", "*.woff2"),
                    "data/knowledge": tuple(f"kb-{index:03}.md" for index in range(1, 9)),
                    "data/fixtures/dev": tuple(f"dev-{index:03}.json" for index in range(1, 9)),
                    "evals/dev": tuple(f"dev-{index:03}.json" for index in range(1, 9)),
                }
                for directory, patterns in selected.items():
                    source = root / directory
                    if not source.is_dir():
                        raise RuntimeError(
                            f"Required public resource directory missing: {directory}"
                        )
                    for path in source.rglob("*"):
                        if path.is_symlink():
                            raise RuntimeError(
                                "Symbolic links are not allowed in release resources"
                            )
                        if path.is_file() and any(path.match(pattern) for pattern in patterns):
                            relative = path.relative_to(root)
                            destination = resource / relative
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(path, destination)
                for name in ("react", "react-dom", "scheduler"):
                    filename = f"{name}-LICENSE.txt"
                    source = root / "third_party/licenses" / filename
                    destination = resource / "frontend/dist/licenses" / filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                if not (resource / "frontend/dist/index.html").is_file():
                    raise RuntimeError(
                        "Build the frontend before creating a release: "
                        "npm run build --prefix frontend"
                    )
                manifest = {
                    path.relative_to(resource).as_posix(): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in sorted(resource.rglob("*"))
                    if path.is_file()
                }
                (resource / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            destination = (
                "incident_desk/resources"
                if self.target_name == "wheel"
                else "src/incident_desk/resources"
            )
            build_data.setdefault("force_include", {})[str(resource)] = destination
        except BaseException:
            shutil.rmtree(temporary)
            raise

    def finalize(self, version, build_data, artifact_path):
        if self._temporary:
            shutil.rmtree(self._temporary, ignore_errors=True)
