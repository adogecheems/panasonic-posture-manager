"""Export AUR source metadata for the exact commit used by a release rebuild."""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[3]


def pin_source(recipe: str, commit: str) -> str:
    if re.fullmatch(r'[0-9a-f]{40}', commit) is None:
        raise ValueError('source commit must be a full Git SHA')
    if recipe.count('#tag=v${pkgver}') != 1:
        raise ValueError('expected one version-tagged AUR source')
    return recipe.replace('#tag=v${pkgver}', f'#commit={commit}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    options = parser.parse_args()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    recipe = (ROOT / 'packaging/linux/aur/PKGBUILD').read_text(encoding='utf-8')
    options.output.write_text(pin_source(recipe, commit), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
