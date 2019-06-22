"""Python to AWS Step Functions compiler."""

import click

from .tools import compile
from .tools import gen_lambda


@click.group()
def cli():
    """Python to AWS Step Functions compiler."""
    pass


cli.add_command(compile.main, name="compile")
cli.add_command(gen_lambda.compile_zipfile, name="lambda")

if __name__ == "__main__":
    cli()
