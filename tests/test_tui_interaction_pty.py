import unittest
from unittest import mock

from test_tui_pty import PtyBackend, PtySession
from test_tui_session import FakeBackend


def record_action(backend, command, args=()):
    FakeBackend.start_action(backend, command, args)
    backend.record()


class InteractionPtyTests(unittest.TestCase):
    def open_session(self, cols=80, rows=24):
        session = PtySession(cols=cols, rows=rows)
        self.addCleanup(session.close)
        session.wait_for(b"Quit")
        return session

    def test_menu_ban_requires_explicit_confirmation(self):
        with mock.patch.object(PtyBackend, "start_action", record_action):
            session = self.open_session()
            session.send(b"a")
            session.wait_for(b"More actions")
            session.send(b"\x1b[B" * 10 + b"\r")
            session.wait_for(b"Ban the wallpaper showing and move to the next one?")
            session.send(b"\r")
            session.wait_for(b"Cancelled")
            self.assertNotIn(b'["action", "ban"', session.events)
            session.send(b"a" + b"\x1b[B" * 10 + b"\ry")
            session.wait_for(b'["action", "ban", ["1", "--expected-pin", "screen-1-pin"]]', events=True)
            session.send(b"q")
            session.wait_for(b"restored:True", events=True)

    def test_escape_then_y_never_confirms(self):
        with mock.patch.object(PtyBackend, "start_action", record_action):
            session = self.open_session()
            session.send(b"x")
            session.wait_for(b"Ban this wallpaper")
            session.send(b"\x1by")
            session.wait_for(b"Cancelled")
            session.send(b"q")
            session.wait_for(b"exit:0", events=True)
            self.assertNotIn(b'"ban"', session.events)

    def test_settings_editor_preserves_case_through_terminal_input(self):
        with mock.patch.object(PtyBackend, "start_action", record_action), \
                mock.patch.object(FakeBackend, "settings", return_value=[("mood", '"off"')]):
            session = self.open_session(36, 10)
            session.send(b",")
            session.wait_for(b"Settings")
            session.send(b"\r")
            session.wait_for(b"mood: ")
            session.send(b"\x15Night\r")
            session.wait_for(b'["action", "config", ["mood", "\\\"Night\\\"", "\\\"off\\\""]]', events=True)
            session.send(b"\x1bq")
            session.wait_for(b"restored:True", events=True)

    def test_search_help_and_favorite_work_through_a_real_terminal(self):
        session = self.open_session()
        session.send(b"/sec")
        session.wait_for(b" 1 of 1 ")
        session.send(b"\r ")
        session.wait_for(b"Added to favorites")
        session.send(b"?")
        session.wait_for(b"Keyboard help")
        session.send(b"q ")
        session.wait_for(b"Removed from favorites")
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)


if __name__ == "__main__":
    unittest.main()
