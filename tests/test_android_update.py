"""Validate update release metadata without exposing embedded credentials or signing keys."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.publish_android_update import manifest_from_badging, upload
from scripts import install_android_update_host as host_setup
from scripts import migrate_android_update_host as migration


class AndroidUpdateTests(unittest.TestCase):
    badging = "package: name='dev.kaiforward.app' versionCode='12' versionName='0.7.0'\nsdkVersion:'26'\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.apk = self.root / 'app.apk'
        self.apk.write_bytes(b'verified release APK')

    def test_manifest_records_actual_apk_metadata_and_relative_download(self):
        result = manifest_from_badging(self.badging, self.apk,
            'https://permafrost06.com/kaiforward/updates/', 'Release notes')
        self.assertEqual(result['versionCode'], 12)
        self.assertEqual(result['sha256'], hashlib.sha256(self.apk.read_bytes()).hexdigest())
        self.assertEqual(result['size'], self.apk.stat().st_size)
        self.assertEqual(result['notes'], 'Release notes')
        self.assertNotIn('/', result['apkUrl'])

    def test_subdomain_is_supported_and_untrusted_hosts_are_rejected(self):
        manifest_from_badging(self.badging, self.apk, 'https://kaiforward.permafrost06.com/updates/', '')
        for url in ('http://permafrost06.com/updates/', 'https://evil.example/updates/',
                    'https://permafrost06.com/updates/?secret=token', 'https://permafrost06.com/updates',
                    'https://user@permafrost06.com/updates/', 'https://permafrost06.com:444/updates/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                manifest_from_badging(self.badging, self.apk, url, '')

    def test_wrong_package_or_unreadable_badging_is_rejected(self):
        for badging in ('bad', self.badging.replace('dev.kaiforward.app', 'other.app'),
                        self.badging.replace("sdkVersion:'26'", "sdkVersion:'25'")):
            with self.assertRaises(ValueError):
                manifest_from_badging(badging, self.apk, 'https://permafrost06.com/updates/', '')

    def test_staging_stays_outside_the_web_root(self):
        with patch('scripts.publish_android_update.run') as run:
            upload(self.root, 'edges-studio@main-ubuntu-sg', initialize=True)
        self.assertIn('mkdir -p -m 700 ~/kaiforward-update-staging', run.call_args_list[0].args)
        self.assertFalse(any('/var/www/' in str(call) for call in run.call_args_list))

    def test_ssh_option_injection_is_rejected(self):
        with patch('scripts.publish_android_update.run') as run:
            for host in ('-oProxyCommand=bad', 'host; command', 'user@host/path'):
                with self.assertRaises(ValueError):
                    upload(self.root, host, initialize=True)
        run.assert_not_called()

    def configure_host_fixture(self):
        public = self.root / 'public'
        public.mkdir()
        (public / 'app.apk').write_bytes(self.apk.read_bytes())
        (public / 'latest.json').write_text(json.dumps({'apkUrl': 'app.apk',
            'sha256': hashlib.sha256(self.apk.read_bytes()).hexdigest()}))
        (self.root / 'auth.htpasswd').write_text('kaiforward:hashed-password\n')
        (self.root / 'nginx-location.conf').write_text('location /kaiforward/updates/ { auth_basic "updates"; }\n')
        self.site = self.root / 'site.conf'
        self.original_site = 'server {\n    root /var/www/permafrost06;\n    location /existing/ { return 200; }\n}\n'
        self.site.write_text(self.original_site)
        self.host_root = self.root / 'srv'
        self.snippet = self.root / 'snippet.conf'
        for patcher in (patch.object(host_setup, 'HOST_ROOT', self.host_root),
                        patch.object(host_setup, 'NGINX_SNIPPET', self.snippet),
                        patch.object(host_setup.os, 'geteuid', return_value=0),
                        patch.object(host_setup.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=os.getgid())),
                        patch.object(host_setup.os, 'chown'),
                        patch.dict(os.environ, SUDO_UID=str(os.getuid()))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_host_setup_preserves_routes_and_keeps_password_outside_public(self):
        self.configure_host_fixture()
        with patch.object(host_setup.subprocess, 'run') as run:
            host_setup.install(self.root, self.site)
        self.assertIn('location /existing/ { return 200; }', self.site.read_text())
        self.assertIn(str(self.snippet), self.site.read_text())
        self.assertTrue((self.host_root / '.initialized').exists())
        self.assertFalse((self.host_root / 'public/auth.htpasswd').exists())
        self.assertEqual((self.host_root / 'auth.htpasswd').stat().st_mode & 0o777, 0o640)
        self.assertEqual(run.call_args_list[0].args[0], ['nginx', '-t'])
        self.assertEqual(run.call_args_list[1].args[0], ['systemctl', 'reload', 'nginx'])

    def test_nginx_failure_restores_configuration_without_reloading(self):
        self.configure_host_fixture()
        self.snippet.write_text('previous snippet')
        with patch.object(host_setup.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'nginx')) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                host_setup.install(self.root, self.site)
        self.assertEqual(self.site.read_text(), self.original_site)
        self.assertEqual(self.snippet.read_text(), 'previous snippet')
        self.assertFalse((self.host_root / '.initialized').exists())
        self.assertEqual(run.call_count, 1)


class SubdomainMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        paths = {'ROOT': self.root / 'updates', 'SITE': self.root / 'available/site',
            'ENABLED': self.root / 'enabled/site', 'LEGACY': self.root / 'legacy.conf',
            'CERTIFICATE': self.root / 'cert', 'CHALLENGES': self.root / 'acme',
            'RENEW_HOOK': self.root / 'hooks/deploy/reload'}
        for name, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            patcher = patch.object(migration, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        migration.ROOT.mkdir()
        (migration.ROOT / '.initialized').touch()
        self.original = 'location /kaiforward/updates/ { auth_basic "updates"; }\n'
        migration.LEGACY.write_text(self.original)
        for patcher in (patch.object(migration.os, 'geteuid', return_value=0),
                        patch.object(migration.socket, 'getaddrinfo', return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def existing_certificate(self):
        migration.CERTIFICATE.mkdir()
        for name in ('fullchain.pem', 'privkey.pem'):
            (migration.CERTIFICATE / name).touch()

    def test_existing_certificate_migrates_with_authentication_and_direct_legacy_access(self):
        self.existing_certificate()
        with patch.object(migration.subprocess, 'run') as run:
            migration.migrate()
        self.assertIn('auth_basic_user_file', migration.SITE.read_text())
        self.assertIn('location ^~ /updates/', migration.SITE.read_text())
        self.assertIn('auth_basic_user_file', migration.LEGACY.read_text())
        self.assertIn(f'alias {migration.ROOT}/public/', migration.LEGACY.read_text())
        self.assertNotIn('rewrite ', migration.LEGACY.read_text())
        self.assertEqual(migration.ENABLED.resolve(), migration.SITE)
        self.assertEqual(run.call_count, 2)
        self.assertIn('systemctl reload nginx', migration.RENEW_HOOK.read_text())

    def test_legacy_redirect_requires_explicit_opt_in_after_dns_propagation(self):
        self.assertIn('https://kaiforward.permafrost06.com/updates/$1 permanent',
            migration.legacy_config(redirect=True))
        self.assertNotIn('rewrite ', migration.legacy_config())

    def test_certificate_failure_preserves_old_endpoint_and_restores_sites(self):
        def execute(command, **kwargs):
            if command[0] == 'certbot':
                self.assertEqual(migration.LEGACY.read_text(), self.original)
                raise subprocess.CalledProcessError(1, command)
        with patch.object(migration.subprocess, 'run', side_effect=execute):
            with self.assertRaises(subprocess.CalledProcessError):
                migration.migrate()
        self.assertEqual(migration.LEGACY.read_text(), self.original)
        self.assertFalse(migration.SITE.exists())
        self.assertFalse(migration.ENABLED.is_symlink())

    def test_final_configuration_failure_rolls_back_redirect(self):
        self.existing_certificate()
        with patch.object(migration.subprocess, 'run', side_effect=[
                subprocess.CalledProcessError(1, 'nginx'), None, None]):
            with self.assertRaises(subprocess.CalledProcessError):
                migration.migrate()
        self.assertEqual(migration.LEGACY.read_text(), self.original)
        self.assertFalse(migration.SITE.exists())
        self.assertFalse(migration.RENEW_HOOK.exists())

    def test_unmanaged_subdomain_configuration_is_preserved(self):
        migration.SITE.write_text('server { # existing custom configuration\n}\n')
        with self.assertRaises(SystemExit):
            migration.migrate()
        self.assertEqual(migration.LEGACY.read_text(), self.original)
        self.assertIn('existing custom configuration', migration.SITE.read_text())


if __name__ == '__main__':
    unittest.main()
