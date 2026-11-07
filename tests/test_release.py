import unittest
from tools.check_release import inspect_file


class ReleaseCheckTests(unittest.TestCase):
    def test_private_binary_and_link_paths_are_rejected(self):
        for name in ('private/identity.json','jobs/resume.docx','tests/__pycache__/cached.pyc','.env','tools/.hidden.py'):
            self.assertIn('path_not_in_public_allowlist', inspect_file(name,b'{}'))
        self.assertIn('symlink_or_submodule_not_allowed',inspect_file('tools/test.py',b'../../private','120000'))
        self.assertIn('binary_content',inspect_file('tests/test.py',bytes([0])))

    def test_credentials_and_personal_paths_are_reported_without_values(self):
        secrets = ['sk-'+'a'*30, 'ghp_'+'b'*36, '-----BEGIN '+'PRIVATE KEY-----',
                   '/'.join(['C:', 'Users', 'personal-name', 'file.txt']), 'person@'+'mail.invalid']
        for secret in secrets:
            self.assertTrue(inspect_file('tools/test.py',secret.encode()))
            self.assertNotIn(secret,str(inspect_file('tools/test.py',secret.encode())))
        self.assertEqual(inspect_file('tests/test.py',b'test@example.test'),[])
        self.assertEqual(inspect_file('README.md',b'C:/Windows/Fonts/msyh.ttc'),[])

if __name__=='__main__':unittest.main()
