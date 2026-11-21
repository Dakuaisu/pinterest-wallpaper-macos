import unittest
from unittest import mock

from test_tui_pty import PtySession
from test_tui_session import FakeBackend, Pin


class VisualPtyTests(unittest.TestCase):
    def open_session(self, cols, rows, items=None):
        original_init = FakeBackend.__init__

        def custom(backend):
            original_init(backend)
            if items is not None:
                backend.items = items

        with mock.patch.object(FakeBackend, "__init__", custom):
            session = PtySession(cols=cols, rows=rows)
        self.addCleanup(session.close)
        return session

    def quit(self, session):
        session.send(b"q")
        session.wait_for(b"exit:0", events=True)
        session.wait_for(b"restored:True", events=True)

    def test_small_window_quit_hint_and_key_work(self):
        session = self.open_session(25, 8)
        session.wait_for(b"q quit | Resize 36x10")
        self.quit(session)

    def test_minimum_window_caption_follows_selection(self):
        session = self.open_session(36, 10)
        session.wait_for(b" First ")
        session.send(b"\x1b[B")
        session.wait_for("90° · fill".encode())
        self.quit(session)

    def test_preview_title_follows_the_original_toggle(self):
        items = [Pin("opaque-first-identifier-0123456789", "800x600 source", 0, False),
                 Pin("opaque-second-identifier-0123456789", "640x480 source", 90, True)]
        session = self.open_session(36, 10, items)
        session.wait_for(b" Preview ")
        session.send(b"\x1b[Bv")
        session.wait_for(b" Original ")
        session.wait_for("640×480".encode())
        self.assertNotIn(b"opaque-", session.output)
        self.quit(session)

    def test_empty_library_message_is_complete_at_minimum_size(self):
        session = self.open_session(36, 10, [])
        session.wait_for(b"No wallpapers ")
        session.wait_for(b"add a board, ")
        self.quit(session)


if __name__ == "__main__":
    unittest.main()
