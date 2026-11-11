"""Install a committed BassBeerMap worker for the existing Hermes cron job."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path


def install(repo, revision, state, launcher):
    repo, state, launcher = (path.resolve() for path in (repo, state, launcher))

    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])

    commit = git('rev-parse', '--verify', revision + '^{commit}').decode().strip()
    sources = [name for name in git('ls-tree', '--name-only', commit).decode().splitlines()
               if name.endswith('.py')]
    payload = git('archive', '--format=tar', commit, '--', *sources)
    app = state / ('app-' + commit)
    hashes = {}
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bass-map-install-', dir=state) as temporary:
        staged = Path(temporary) / 'app'
        staged.mkdir()
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for name in sources:
                data = archive.extractfile(name).read()
                compile(data, name, 'exec')
                (staged / name).write_bytes(data)
                hashes[name] = hashlib.sha256(data).hexdigest()
        manifest = {'commit': commit, 'files': hashes}
        (staged / 'implementation.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        if app.exists():
            actual = {name: hashlib.sha256((app / name).read_bytes()).hexdigest() for name in sources}
            if actual != hashes or json.loads((app / 'implementation.json').read_text()) != manifest:
                raise ValueError(f'Installed revision has modified files: {app}')
        else:
            os.replace(staged, app)

    code = ('"""Launch the installed BassBeerMap Google review worker."""\n'
            'import sys\n'
            f'sys.path.insert(0, {str(app)!r})\n'
            'from hermes_google_runner import main\n'
            'main()\n')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=launcher.parent, delete=False) as handle:
        handle.write(code)
        staged_launcher = Path(handle.name)
    try:
        os.replace(staged_launcher, launcher)
    finally:
        staged_launcher.unlink(missing_ok=True)
    return commit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    args = parser.parse_args()
    print(install(Path(__file__).resolve().parent, args.revision, args.state_dir, args.launcher))
