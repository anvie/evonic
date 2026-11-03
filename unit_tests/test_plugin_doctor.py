import types
import unittest

from backend.plugin_doctor import Finding, run_plugin_doctor


def _module(**fns):
    return types.SimpleNamespace(**fns)


class RunPluginDoctorTest(unittest.TestCase):
    def test_plugin_without_hook_is_skipped(self):
        self.assertEqual(run_plugin_doctor(plugins=[('p', 'P', _module(), {})]), [])

    def test_check_findings_are_reported_without_fixing(self):
        fixed = []
        mod = _module(doctor_check=lambda ctx: [Finding.warn('lacks tools', fixable=True, agent_id='a1')],
                      doctor_fix=lambda ctx, findings: fixed.append(findings) or ['done'])
        [report] = run_plugin_doctor(fix=False, plugins=[('p', 'P', mod, {})])
        self.assertEqual([f.message for f in report['findings']], ['lacks tools'])
        self.assertEqual(report['fixes'], [])
        self.assertEqual(fixed, [])

    def test_fix_receives_only_fixable_non_ok_findings(self):
        received = []
        mod = _module(
            doctor_check=lambda ctx: [Finding.ok('fine'), Finding.warn('manual'),
                                      Finding.warn('auto', fixable=True, agent_id='a1')],
            doctor_fix=lambda ctx, findings: received.extend(findings) or ['assigned to a1'])
        [report] = run_plugin_doctor(fix=True, plugins=[('p', 'P', mod, {})])
        self.assertEqual([f.message for f in received], ['auto'])
        self.assertEqual(report['fixes'], ['assigned to a1'])

    def test_hook_that_raises_becomes_a_failed_finding(self):
        def boom(ctx):
            raise RuntimeError('nope')
        [report] = run_plugin_doctor(plugins=[('p', 'P', _module(doctor_check=boom), {})])
        self.assertEqual(report['findings'][0].level, 'fail')
        self.assertIn('nope', report['findings'][0].message)

    def test_fix_that_raises_is_reported_and_keeps_the_check_findings(self):
        def boom(ctx, findings):
            raise RuntimeError('cannot')
        mod = _module(doctor_check=lambda ctx: [Finding.warn('x', fixable=True)], doctor_fix=boom)
        [report] = run_plugin_doctor(fix=True, plugins=[('p', 'P', mod, {})])
        self.assertEqual([f.level for f in report['findings']], ['warn', 'fail'])

    def test_ctx_carries_plugin_id_config_and_fix_flag(self):
        seen = {}
        mod = _module(doctor_check=lambda ctx: seen.update(vars(ctx)) or [])
        run_plugin_doctor(fix=True, plugins=[('p', 'P', mod, {'K': 'v'})])
        self.assertEqual(seen, {'plugin_id': 'p', 'config': {'K': 'v'}, 'fix': True})


if __name__ == '__main__':
    unittest.main()
