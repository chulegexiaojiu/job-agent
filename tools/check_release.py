"""Check the Git index for private paths and common sensitive values.

Prints no matched values. Supplements manual review, not an exhaustive guarantee.
"""
from __future__ import annotations
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'.gitignore', 'README.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md',
              'CHANGELOG.md', 'requirements.txt', 'requirements-probe.txt', 'start.cmd', 'check-api.cmd'}
TREES = {'job_agent': {'.py'}, 'tools': {'.py'}, 'tests': {'.py'},
         'examples': {'.json'}, 'docs': {'.md'}, '.github': {'.yml', '.yaml', '.md'}}
PATTERNS = {
    'credential_like_value': re.compile(r'(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16})'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'personal_windows_path': re.compile('[A-Za-z]:/Users/[^/ ]+', re.I),
}
EMAIL = re.compile(r'[A-Za-z0-9_.+%-]+@([A-Za-z0-9.-]+[.][A-Za-z]{2,})')


def inspect_file(name: str, data: bytes, mode: str = '100644') -> list[str]:
    path = PurePosixPath(name)
    allowed = (name in ROOT_FILES or (len(path.parts) > 1 and path.parts[0] in TREES
               and path.suffix in TREES[path.parts[0]]
               and not any(part.startswith('.') or part == '__pycache__' for part in path.parts[1:])))
    reasons = [] if allowed else ['path_not_in_public_allowlist']
    if mode not in {'100644', '100755'}: reasons.append('symlink_or_submodule_not_allowed')
    if len(data) > 1024 * 1024: reasons.append('oversized_file')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        return reasons + ['non_utf8_or_binary_file']
    if chr(0) in text: reasons.append('binary_content')
    reasons += [label for label, pattern in PATTERNS.items() if pattern.search(text.replace(chr(92), '/'))]
    for match in EMAIL.finditer(text):
        domain = match.group(1).lower()
        if domain not in {'example.com', 'example.org', 'example.net', 'users.noreply.github.com'} and not domain.endswith('.test'):
            reasons.append('non_example_email'); break
    return reasons


def main() -> int:
    def git(*args):
        return subprocess.run(['git', *args], cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    try:
        entries = git('ls-files', '--stage', '-z').decode('utf-8').split(chr(0))
        findings = []; count = 0
        for entry in filter(None, entries):
            metadata, name = entry.split(chr(9), 1)
            mode, oid, stage = metadata.split()
            reasons = inspect_file(name, git('cat-file', 'blob', oid), mode)
            if stage != '0': reasons.append('unmerged_file')
            count += 1
            if reasons: findings.append({'path': name, 'reasons': reasons})
        if not count: findings.append({'path': '.', 'reasons': ['empty_index']})
        result = {'status': 'pass' if not findings else 'blocked', 'files_checked': count,
                  'scope': 'git_index_only', 'findings': findings}
    except (OSError, ValueError, subprocess.CalledProcessError):
        result = {'status': 'error', 'reason': 'Cannot inspect the Git index.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
