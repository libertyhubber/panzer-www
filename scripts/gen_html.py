import sys
import jinja2 as j2
import pathlib as pl


def main(args: list[str]) -> int:
    template_name = args[0]

    env = j2.Environment(loader=j2.FileSystemLoader('templates'))

    template = env.get_template(template_name)

    with pl.Path(template_name).open(mode="w") as fobj:
        fobj.write(template.render())

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

