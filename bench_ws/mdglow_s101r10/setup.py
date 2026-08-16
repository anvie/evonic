from setuptools import setup, find_packages

setup(
    name='mdglow-cli-s101r10',
    version='0.1.0',
    packages=find_packages(),
    install_requires=[],
    entry_points={
        'console_scripts': [
            'mdglow-cli-s101r10 = cli:main',
        ],
    },
)
