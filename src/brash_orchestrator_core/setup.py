from pathlib import Path

from setuptools import find_packages, setup

PACKAGE_NAME = "brash_orchestrator_core"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _shared(subdirectory: str, pattern: str) -> tuple[str, list[str]]:
    """Install repository-level configuration next to the installed package."""
    source = REPO_ROOT / subdirectory
    files = sorted(str(path) for path in source.glob(pattern) if path.is_file())
    return (f"share/{PACKAGE_NAME}/{subdirectory}", files)


setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=["test", "test.*"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        _shared("config", "*.yaml"),
        _shared("schemas", "*.json"),
        _shared("prompts", "*.md"),
        _shared("prompts", "*.jsonl"),
    ],
    # The console's browser assets are served from disk, so this package can no
    # longer be imported from a zip.
    package_data={f"{PACKAGE_NAME}.web": ["static/*"]},
    install_requires=["setuptools", "PyYAML", "jsonschema", "aiohttp"],
    zip_safe=False,
    maintainer="Brash Robotics",
    maintainer_email="robotics@brash.dev",
    description="LLM task interpreter, validator, scheduler, and execution manager for BrashOrchestrator.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            f"orchestrator_node = {PACKAGE_NAME}.nodes.orchestrator_node:main",
            f"brash_orchestrator = {PACKAGE_NAME}.cli:main",
            f"brash_orchestrator_console = {PACKAGE_NAME}.web.__main__:main",
        ],
    },
)
