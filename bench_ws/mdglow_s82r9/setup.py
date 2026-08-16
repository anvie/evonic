from setuptools import setup, find_packages

setup(
    name='mdglow-cli-s82r9',
    version='0.1.0',
    packages=find_packages(),
    install_requires=[],
    entry_points={
        'console_scripts': [
            'mdglow-cli-s82r9=cli:main',
        ],
    },
)
