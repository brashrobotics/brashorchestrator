from setuptools import find_packages, setup

PACKAGE_NAME = "brash_world_model"

setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=["test", "test.*"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
    ],
    install_requires=["setuptools", "PyYAML"],
    zip_safe=True,
    maintainer="Brash Robotics",
    maintainer_email="robotics@brash.dev",
    description="Symbolic world state for the Brash orchestration layer.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            f"world_model_node = {PACKAGE_NAME}.nodes.world_model_node:main",
            f"embodied_telemetry_node = {PACKAGE_NAME}.nodes.embodied_telemetry:main",
        ],
    },
)
