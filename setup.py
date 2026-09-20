from setuptools import setup, find_packages

setup(
    name="retchat",
    version="0.1.0",
    description="Modern Reticulum Network LXMF Chat Client in GTK4 + Libadwaita",
    author="stereo",
    packages=find_packages(),
    package_data={
        "retchat": ["style.css"],
    },
    include_package_data=True,
    install_requires=[
        "rns>=1.5.0",
        "lxmf>=1.1.0",
        "msgpack",
    ],
    entry_points={
        "gui_scripts": [
            "retchat=retchat.app:main",
        ],
        "console_scripts": [
            "retchat=retchat.app:main",
        ],
    },
)
