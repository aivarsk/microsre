#!/usr/bin/env python3
import tomllib
import zipapp
import shutil
import sys
from pathlib import Path

def build():
    # 1. Read pyproject.toml
    with open("pyproject.toml", "rb") as f:
        config = tomllib.load(f)

    project = config["project"]
    name = project["name"]
    version = project["version"]

    # 2. Prepare build directory
    build_dir = Path("build_zipapp")
    if build_dir.exists():
        shutil.rmtree(build_dir)

    # 3. Copy your package source
    # Assumes your code is in a directory matching the package name
    pkg_src = Path("src") / name.replace("-", "_")

    shutil.copytree(pkg_src, build_dir / pkg_src.name)
    shutil.copy("LICENSE", build_dir / pkg_src.name)

    # 5. Generate .dist-info/METADATA
    dist_info_name = f"{name}-{version}.dist-info"
    dist_info_dir = build_dir / dist_info_name
    dist_info_dir.mkdir()

    metadata = f"""Metadata-Version: 2.1
Name: {name}
Version: {version}
"""
    if "description" in project:
        metadata += f"Summary: {project['description']}\n"
    if "authors" in project and project["authors"]:
        metadata += f"Author: {project['authors'][0]['name']}\n"

    (dist_info_dir / "METADATA").write_text(metadata)

    # 6. Build the zipapp
    output = f"dist/{name}-{version}.pyz"
    zipapp.create_archive(
        build_dir,
        output,
        main=f"{name}.cli:main",
        interpreter="/usr/bin/env python3",
        compressed=True,
    )

    print(f"Built: {output}")
    # Cleanup
    shutil.rmtree(build_dir)

if __name__ == "__main__":
    build()
