from setuptools import setup, find_packages

setup(
    name="orders-sdk-s101r11",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "requests",
    ],
    description="Python client SDK for the Order Service API.",
    author="Aisyah",
)
