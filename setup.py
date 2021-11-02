import re
from codecs import open
from os import path

from setuptools import find_packages, setup

PACKAGE_NAME = "discord"
HERE = path.abspath(path.dirname(__file__))

with open("README.md", "r", encoding="UTF-8") as f:
    README = f.read()
with open(path.join(HERE, PACKAGE_NAME, "base.py"), encoding="utf-8") as fp:
    VERSION = re.search('__version__ = "([^"]+)"', fp.read()).group(1)

extras = {
    "lint": ["black", "flake8", "isort"],
    "readthedocs": ["sphinx", "karma-sphinx-theme"],
}
extras["lint"] += extras["readthedocs"]
extras["dev"] = extras["lint"] + extras["readthedocs"]

setup(
    name="discord-continued",
    version=VERSION,
    author="sakurazaki",
    author_email="aitormartin1990@gmail.com",
    description="A simple API wrapper for Discord.",
    extras_require=extras,
    install_requires=["pre-commit", "aiohttp", "orjson"],
    license="MIT License",
    long_description=README,
    url="https://github.com/sakurazaki/discord-continued",
    packages=find_packages(),
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Topic :: Internet",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Software Development :: Libraries",
        "Topic :: Utilities",
    ],
)
