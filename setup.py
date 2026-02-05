from setuptools import setup, find_packages

setup(
    name="flow",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "fastmcp",
        "docker",
        "python-dotenv",
    ],
    entry_points={
        "console_scripts": [
            "flow = flow.server:main",
        ],
    },
)
