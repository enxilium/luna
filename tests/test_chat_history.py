"""Local history, native file drops, and continuity of the island's shared spring."""

from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import AppKit as AK
from Foundation import NSURL
import Quartz as Q

from src.ui.chats import ChatStore
from src.ui.drop import file_paths
from src.ui.motion import Spring
from src.ui.notch import NotchWindow
from src.ui import app as ui_app


class StoreTests(unittest.TestCase):
    def test_gui_rejects_a_second_instance_and_releases_its_lock_after_exit_or_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ui_app, "CHAT_PATH", Path(folder) / "chats.sqlite3"):
                def running(**kwargs):
                    with self.assertRaisesRegex(RuntimeError, "already running"):
                        ui_app.run_app(preview=True)

                with patch.object(ui_app, "_run_app", side_effect=running) as launch:
                    ui_app.run_app(preview=True)
                    self.assertEqual(launch.call_count, 1)
                with patch.object(ui_app, "_run_app", side_effect=ValueError("startup failed")):
                    with self.assertRaises(ValueError):
                        ui_app.run_app(preview=True)
                with patch.object(ui_app, "_run_app") as launch:
                    ui_app.run_app(preview=True)
                    launch.assert_called_once_with(preview=True)

    def test_chats_messages_drafts_and_file_references_survive_reopening(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chats.sqlite3"
            store = ChatStore(path)
            first = store.active
            store.send("Plan my afternoon", ["/tmp/notes.pdf"], "A saved reply")
            second = store.new()
            store.save_draft("An unfinished message", ["/tmp/image.png"])
            store.close()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reopened = ChatStore(path)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.active, second)
            self.assertEqual(reopened.current["draft"], "An unfinished message")
            self.assertEqual(reopened.current["files"], ["/tmp/image.png"])
            self.assertEqual(len(reopened.conversations()), 2)
            reopened.select(first)
            messages = reopened.messages()
            self.assertEqual([m["role"] for m in messages], ["user", "luna"])
            self.assertEqual(messages[0]["files"], ["/tmp/notes.pdf"])
            self.assertEqual(messages[1]["text"], "A saved reply")
            self.assertEqual(reopened.current["title"], "Plan my afternoon")

    def test_failed_send_rolls_back_and_two_windows_do_not_overwrite_other_chats(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chats.sqlite3"
            one = ChatStore(path)
            self.addCleanup(one.close)
            one.save_draft("Keep this draft", ["/tmp/a.pdf"])
            one.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):
                one.send("Must not disappear", [], "Reply")
            one.db.set_authorizer(None)
            self.assertEqual(one.messages(), [])
            self.assertEqual(one.current["draft"], "Keep this draft")
            two = ChatStore(path)
            self.addCleanup(two.close)
            two.new()
            two.send("Second chat", [], "Second reply")
            one.send("First chat", [], "First reply")
            self.assertEqual(len(one.conversations()), 2)
            self.assertEqual(one.messages()[0]["text"], "First chat")
            self.assertEqual(two.messages()[0]["text"], "Second chat")

    def test_corrupt_or_future_history_is_never_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chats.sqlite3"
            path.write_bytes(b"This is not a database")
            with self.assertRaises(sqlite3.DatabaseError):
                ChatStore(path)
            self.assertEqual(path.read_bytes(), b"This is not a database")
            path.unlink()
            db = sqlite3.connect(path)
            db.execute("PRAGMA user_version = 99")
            db.close()
            with self.assertRaisesRegex(RuntimeError, "newer version"):
                ChatStore(path)
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
            db.close()


class ChatInteractionTests(unittest.TestCase):
    def setUp(self):
        AK.NSApplication.sharedApplication()
        self.notch = NotchWindow.alloc().init()
        self.notch.build()
        self.addCleanup(self.notch.close)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = Path(self.folder.name).resolve() / "notes.txt"
        self.file.write_text("A file for the drop test")

    def type(self, text):
        self.notch.chat.prompt.setString_(text)
        self.notch.chat.textDidChange_(None)

    def finish_reply(self):
        _, text, start = self.notch.chat.pending
        self.notch.chat.tick(start + len(text) / 75 + 1)

    def test_history_selection_restores_per_chat_drafts_files_and_pending_reply(self):
        n = self.notch
        first = n.chat.store.active
        self.type("First conversation")
        n.chat.send_(None)
        first_reply = n.chat.pending[1]
        self.type("Draft for the first chat")
        n.chat.attach_paths([self.file])
        n.newChat_(None)
        second = n.chat.store.active
        self.assertNotEqual(first, second)
        self.assertFalse(n.chat.messages)
        self.assertFalse(n.chat.files)
        self.type("Draft for the second chat")
        n.showHistory_(None)
        n._draw(animate=False)
        self.assertEqual(n.mode, "history")
        self.assertFalse(n.history_button.isHidden())
        self.assertFalse(n.new_button.isHidden())
        first_row = next(row for row in n.history.rows if row.chat_id == first)
        n.history.selectChat_(first_row)
        self.assertEqual(n.chat.store.active, first)
        self.assertEqual(n.chat.prompt.string(), "Draft for the first chat")
        self.assertEqual(n.chat.files, [str(self.file)])
        self.finish_reply()
        self.assertEqual(n.chat.messages[-1].text, first_reply)
        self.assertTrue(n.chat.select_chat(second))
        self.assertEqual(n.chat.prompt.string(), "Draft for the second chat")
        self.assertEqual(n.chat.messages, [])

    def test_real_file_url_pasteboard_attaches_to_only_the_next_message(self):
        n = self.notch
        board = AK.NSPasteboard.pasteboardWithUniqueName()
        board.writeObjects_([NSURL.fileURLWithPath_(str(self.file))])
        sender = SimpleNamespace(draggingPasteboard=lambda: board,
                                 draggingLocation=lambda: (n.canvas_width / 2, n.canvas_height - 8))
        surface = n.panel.contentView()
        self.assertEqual(surface.draggingEntered_(sender), AK.NSDragOperationCopy)
        n._draw(animate=False)
        self.assertTrue(n.drop_hover)
        self.assertFalse(n.drop_view.isHidden())
        self.assertTrue(surface.prepareForDragOperation_(sender))
        self.assertTrue(surface.performDragOperation_(sender))
        self.assertEqual(n.chat.files, [str(self.file)])
        self.assertFalse(n.drop_hover)
        self.assertTrue(n.chat.send_button.isEnabled())
        self.assertNotIn(str(self.file), n.chat.prompt.string())
        n.chat.send_(None)
        self.assertEqual(n.chat.files, [])
        self.assertEqual(n.chat.store.messages()[0]["files"], [str(self.file)])
        self.assertEqual(n.chat.messages[0].files, (str(self.file),))
        self.finish_reply()
        self.type("A follow-up without a file")
        n.chat.send_(None)
        self.assertEqual(n.chat.store.messages()[2]["files"], [])
        self.assertTrue(self.file.exists())

    def test_duplicate_remove_missing_file_and_failed_send_preserve_the_draft(self):
        n = self.notch
        self.assertTrue(n.chat.attach_paths([self.file, self.file.parent / "./notes.txt"]))
        self.assertEqual(len(n.chat.files), 1)
        n.chat.removeAttachment_(SimpleNamespace(tag=lambda: 0))
        self.assertEqual(n.chat.files, [])
        self.assertTrue(self.file.exists())
        n.chat.attach_paths([self.file])
        self.type("Keep this message")
        with patch.object(n.chat.store, "send", side_effect=sqlite3.OperationalError("disk unavailable")):
            n.chat.send_(None)
        self.assertEqual(n.chat.prompt.string(), "Keep this message")
        self.assertEqual(n.chat.files, [str(self.file)])
        self.assertFalse(n.chat.messages)
        self.file.unlink()
        n.chat.send_(None)
        self.assertEqual(n.chat.prompt.string(), "Keep this message")
        self.assertIn("no longer available", n.chat.status)

    def test_drop_while_replying_belongs_to_the_following_send(self):
        n = self.notch
        self.type("First request")
        n.chat.send_(None)
        n.chat.attach_paths([self.file])
        self.type("Next request")
        n.chat.send_(None)
        self.assertEqual(len(n.chat.store.messages()), 2)
        self.assertEqual(n.chat.store.messages()[0]["files"], [])
        self.finish_reply()
        n.chat.send_(None)
        self.assertEqual(n.chat.store.messages()[2]["files"], [str(self.file)])

    def test_drag_hover_expands_from_closed_without_stealing_focus_and_cancel_releases_it(self):
        n = self.notch
        board = AK.NSPasteboard.pasteboardWithUniqueName()
        n.drag_count = board.changeCount()
        board.clearContents()
        board.writeObjects_([NSURL.fileURLWithPath_(str(self.file))])
        frame = n.panel.frame()
        point = (frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height - 8)
        now = time.monotonic()
        n.update_drag(point=point, buttons=1, pasteboard=board, now=now)
        self.assertTrue(n.drop_hover)
        self.assertFalse(n.panel.ignoresMouseEvents())
        self.assertFalse(n.panel.isKeyWindow())
        n.update_drag(point=(0, 0), buttons=0, pasteboard=board, now=now + 0.1)
        n.update_drag(point=(0, 0), buttons=0, pasteboard=board, now=now + 1)
        self.assertFalse(n.drop_hover)
        self.assertEqual(n.mode, "closed")
        self.assertFalse(n.chat.files)
        remote = AK.NSPasteboard.pasteboardWithUniqueName()
        remote.writeObjects_([NSURL.URLWithString_("file://untrusted-host/private/file.txt")])
        self.assertEqual(file_paths(remote), [])

    def test_shape_mask_controls_and_composer_share_each_intermediate_frame(self):
        n = self.notch
        n.open_chat()
        n.full_chat = True
        n._draw(animate=True)
        for index in range(35):
            if index == 8:
                before, velocity = n.motion.value, n.motion.velocity
                n.dismiss()
                self.assertEqual(n.motion.value, before)
                self.assertEqual(n.motion.velocity, velocity)
            n.motion.advance(1 / 60)
            n._render_geometry()
            bounds = Q.CGPathGetPathBoundingBox(n.shape.path())
            self.assertEqual(bounds, Q.CGPathGetPathBoundingBox(n.clip.path()))
            self.assertAlmostEqual(bounds.origin.y + bounds.size.height, n.canvas_height)
            header_left = (n.canvas_width - max(bounds.size.width, n.width)) / 2
            self.assertAlmostEqual(n.moon.position().x, header_left + 30)
            self.assertAlmostEqual(n.history_button.frame().origin.x,
                                   n.canvas_width - header_left - 44)
            self.assertAlmostEqual(n.chat.view.frame().size.width, max(1, bounds.size.width - 32))

    def test_reduce_motion_settles_and_streaming_bubble_grows_from_its_top_edge(self):
        n = self.notch
        with patch.object(AK, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(
            accessibilityDisplayShouldReduceMotion=lambda: True,
        ))):
            n.open_chat()
            self.assertFalse(n.motion.moving)
            self.assertEqual(n.motion.value, n.motion.target)
        bubble = n.chat.append("luna", "A short reply")
        n.chat.layout_messages()
        before = bubble.frame()
        bubble.text = "A longer reply that wraps onto several lines. " * 5
        n.chat.layout_messages()
        self.assertEqual(bubble.frame(), before)
        self.assertGreater(bubble.motion.target[3], before.size.height)
        n.chat.tick(n.chat.message_time + 1 / 60)
        current = bubble.frame()
        self.assertGreater(current.size.height, before.size.height)
        self.assertLess(current.size.height, bubble.motion.target[3])
        self.assertEqual(current.origin.y, before.origin.y)
        label = bubble.label.frame()
        self.assertAlmostEqual(current.size.height - label.origin.y - label.size.height, 11)


class MotionTests(unittest.TestCase):
    def test_retarget_preserves_position_and_velocity_and_settles_without_a_jump(self):
        spring = Spring((197, 32))
        spring.move_to((520, 440))
        frames = [spring.advance(1 / 60) for _ in range(12)]
        before, velocity = spring.value, spring.velocity
        spring.move_to((294, 112))
        self.assertEqual(spring.value, before)
        self.assertEqual(spring.velocity, velocity)
        frames += [spring.advance(1 / 60) for _ in range(120)]
        self.assertEqual(spring.value, (294, 112))
        self.assertLess(max(abs(b[0] - a[0]) for a, b in zip(frames, frames[1:])), 40)
        self.assertTrue(all(0 < value[0] < 545 and 0 < value[1] < 456 for value in frames))

    def test_spring_has_consistent_timing_at_different_refresh_rates(self):
        values = []
        for rate in (30, 60, 120):
            spring = Spring((0,))
            spring.move_to((100,))
            for _ in range(rate // 2):
                spring.advance(1 / rate)
            values.append(spring.value[0])
        self.assertLess(max(values) - min(values), 0.001)
