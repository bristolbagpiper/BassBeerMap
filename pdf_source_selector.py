"""Select a precise source PDF for an import workflow run."""
import json
import os
import subprocess
import sys
from pathlib import Path


CANONICAL_PDF = 'latest-bass-directory.pdf'


def changed_pdfs(before, after, run=None):
    if not before or set(before) == {'0'}:
        raise RuntimeError('Cannot identify changed PDFs without the push base commit.')
    if not after or set(after) == {'0'}:
        raise RuntimeError('The push event has no usable head commit.')
    run = run or subprocess.check_output
    raw = run(['git', 'diff', '--name-status', '-z', '--no-renames', before, after])
    fields = raw.split(b'\0')
    result = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        if index >= len(fields):
            raise RuntimeError('Git returned an incomplete changed-file list.')
        path = fields[index].decode('utf-8', errors='surrogateescape')
        index += 1
        if path.lower().endswith('.pdf'):
            result.append((status.decode('ascii'), path))
    return result


def select(event_name, event, *, workspace, runner_temp, run=None):
    workspace = Path(workspace)
    runner_temp = Path(runner_temp)
    if event_name in ('schedule', 'workflow_dispatch'):
        return {'mode': 'wordpress', 'source_pdf': str(workspace / CANONICAL_PDF), 'source_name_file': ''}
    if event_name != 'push':
        return {'mode': 'skip', 'source_pdf': '', 'source_name_file': ''}

    changes = changed_pdfs(event.get('before'), event.get('after'), run=run)
    if not changes:
        return {'mode': 'skip', 'source_pdf': '', 'source_name_file': ''}
    if len(changes) > 1:
        names = ', '.join(path for _, path in changes)
        raise RuntimeError(f'Ambiguous PDF submission; change exactly one PDF per push: {names}')

    status, name = changes[0]
    if '/' in name or '\\' in name:
        raise RuntimeError(f'PDF submissions must be in the repository root: {name}')
    if status == 'D':
        if name == CANONICAL_PDF:
            raise RuntimeError('The canonical directory PDF cannot be deleted.')
        return {'mode': 'skip', 'source_pdf': '', 'source_name_file': ''}
    if status not in ('A', 'M'):
        raise RuntimeError(f'Unsupported PDF change status: {status}')
    if name == CANONICAL_PDF:
        return {'mode': 'canonical_release', 'source_pdf': str(workspace / CANONICAL_PDF), 'source_name_file': ''}

    after = event.get('after')
    blob = (run or subprocess.check_output)(['git', 'show', f'{after}:{name}'], cwd=workspace)
    if not blob.startswith(b'%PDF-'):
        raise RuntimeError(f'The changed upload is not a PDF: {name}')
    runner_temp.mkdir(parents=True, exist_ok=True)
    source_pdf = runner_temp / 'bass-directory-upload.pdf'
    source_pdf.write_bytes(blob)
    source_name_file = runner_temp / 'bass-directory-upload-name.json'
    source_name_file.write_text(json.dumps(name), encoding='utf-8')
    return {'mode': 'upload', 'source_pdf': str(source_pdf), 'source_name_file': str(source_name_file)}


def main():
    event_path = Path(os.environ['GITHUB_EVENT_PATH'])
    event = json.loads(event_path.read_text(encoding='utf-8'))
    selection = select(os.environ.get('GITHUB_EVENT_NAME', ''), event,
                       workspace=os.environ['GITHUB_WORKSPACE'],
                       runner_temp=os.environ['RUNNER_TEMP'])
    output = Path(os.environ['GITHUB_OUTPUT'])
    with output.open('a', encoding='utf-8') as stream:
        for key, value in selection.items():
            stream.write(f'{key}={value}\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a', encoding='utf-8') as stream:
            stream.write(f"## PDF source selection\n\n- Mode: `{selection['mode']}`\n")
    print(f"Selected PDF source mode: {selection['mode']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'PDF source selection failed: {type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(1)
