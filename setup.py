from setuptools import find_packages, setup

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="backup-projects",
    version="0.1.0",
    description="YAML-driven project backups with rsync",
    long_description=long_description,
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=find_packages(exclude=("tests",)),
    install_requires=["PyYAML>=6.0"],
    extras_require={
        "dev": ["pytest>=7.0"],
    },
    entry_points={
        "console_scripts": [
            "backup-projects=backup_projects.cli:main",
        ],
    },
)
