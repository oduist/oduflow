from setuptools import setup, find_packages

setup(
    name="oduflow",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "fastmcp",
        "docker",
        "python-dotenv",
        "packaging",
    ],
    entry_points={
        "console_scripts": [
            "oduflow = oduflow.server:main",
        ],
    },
)
