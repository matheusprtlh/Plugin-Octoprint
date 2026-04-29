from pathlib import Path

from setuptools import setup


plugin_identifier = "marketplace_link"
plugin_package = "octoprint_marketplace_link"
plugin_name = "OctoPrint-MarketplaceLink"
plugin_version = "0.3.0"
plugin_description = (
    "Posta status da impressora num webhook configurável a cada evento."
)

_readme = Path(__file__).parent / "README.md"
plugin_long_description = _readme.read_text(encoding="utf-8") if _readme.exists() else ""


setup(
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    long_description=plugin_long_description,
    long_description_content_type="text/markdown",
    author="Marketplace",
    license="AGPLv3",
    packages=[plugin_package],
    package_data={
        plugin_package: [
            "templates/*.jinja2",
            "static/js/*.js",
            "static/css/*.css",
        ]
    },
    include_package_data=True,
    zip_safe=False,
    install_requires=["requests>=2.28"],
    python_requires=">=3.9,<3.13",
    entry_points={
        "octoprint.plugin": [
            f"{plugin_identifier} = {plugin_package}",
        ],
    },
)
