from setuptools import setup, find_packages

setup(
    name='mdglow-cli-s81r9',
    version='0.1.0',
    py_modules=['cli', 'mdglow'],
    install_requires=[],
    entry_points={
        'console_scripts': [
            'mdglow-cli=cli:main',
        ],
    },
    description='A command-line wrapper for the mdglow library.',
    author='Aisyah',
)
