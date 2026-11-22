"""Local history, native file drops, and continuity of the island's shared spring."""

from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import AppKit as AK
from Foundation import NSURL
import Quartz as Q

from src.agent.text import BRAIN_MODEL, FAST_MODEL, TextEvent
from src.ui.chats import ChatStore
from src.ui.drop import file_paths
from src.ui.glass import PAD
from src.ui.motion import Spring
from src.ui.notch import NotchWindow
from src.ui import app as ui_app


class StoreTests(unittest.TestCase):
    def setUp(self):
        from src import setup

        patcher = patch.object(setup, "DOTENV", setup.DOTENV)  # run_app turns it off for the process
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_luna_runs_the_dashboard_too_and_shares_its_single_instance_lock(self):
        from src.dashboard import window

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ui_app, "CHAT_PATH", Path(folder) / "chats.sqlite3"), \
                    patch.object(window, "DASHBOARD_PATH", Path(folder) / "dashboard.sqlite3"):
                with patch.object(ui_app, "_run_app") as launch:
                    ui_app.run_app()
                    launch.assert_called_once_with(dashboard=True, demo=False)
                # A dashboard already open over your data (run on its own) keeps Luna from starting a second one.
                with window.single_instance():
                    with patch.object(ui_app, "_run_app") as launch:
                        with self.assertRaisesRegex(RuntimeError, "dashboard is already open"):
                            ui_app.run_app()
                        launch.assert_not_called()
                    with patch.object(ui_app, "_run_app") as launch:
                        ui_app.run_app(demo=True)  # demo data lives in memory, so it doesn't count
                        launch.assert_called_once_with(dashboard=True, demo=True)

    def test_one_app_quits_together_and_the_dock_icon_shows_the_dashboard(self):
        import queue

        controller = ui_app.LunaApp.alloc().init()
        controller.commands, controller.events = queue.Queue(), queue.Queue()
        controller.dashboard = SimpleNamespace(hide=Mock(), show=Mock())
        # An ordinary window: switching apps (or answering a permission prompt) never hides it; the Dock icon shows it.
        self.assertFalse(controller.respondsToSelector_("applicationDidResignActive:"))
        self.assertTrue(controller.applicationShouldHandleReopen_hasVisibleWindows_(None, False))
        controller.dashboard.show.assert_called_once()
        # Quitting from anywhere stops the voice pipeline first; its thread ending stops the app and the dashboard.
        self.assertEqual(controller.applicationShouldTerminate_(None), ui_app.AK.NSTerminateCancel)
        self.assertIsNone(controller.commands.get_nowait())

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
            # A chat with only a draft isn't listed in history; it has nothing to reopen yet.
            self.assertEqual([c["title"] for c in reopened.conversations()], ["Plan my afternoon"])
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
        self.requests = []
        self.notch = NotchWindow.alloc().init()
        self.notch.build(submit=self.requests.append)
        self.addCleanup(self.notch.close)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = Path(self.folder.name).resolve() / "notes.txt"
        self.file.write_text("A file for the drop test")

    def type(self, text):
        self.notch.chat.prompt.setString_(text)
        self.notch.chat.textDidChange_(None)

    def finish_reply(self, request=None, text="Done.", status="complete", model=FAST_MODEL, error="", steps=()):
        request = request or self.requests[-1]
        self.notch.chat.receive(TextEvent(request.chat_id, request.message_id, text, status, model, error, steps))

    def test_history_selection_restores_per_chat_drafts_files_and_pending_reply(self):
        n = self.notch
        first = n.chat.store.active
        self.type("First conversation")
        n.chat.send_(None)
        first_request = self.requests[-1]
        self.type("Draft for the first chat")
        n.chat.attach_paths([self.file])
        n.newChat_(None)
        second = n.chat.store.active
        self.assertNotEqual(first, second)
        self.assertFalse(n.chat.messages)
        self.assertFalse(n.chat.files)
        self.type("Draft for the second chat")
        n.chat.receive(TextEvent(first_request.chat_id, first_request.message_id, "Partial", "pending", FAST_MODEL))
        self.assertEqual(n.chat.messages, [])
        n.showHistory_(None)
        n._draw(animate=False)
        self.assertEqual(n.mode, "history")
        # History is compact: the wing controls stay, the chat toolbar title does not appear.
        self.assertTrue(n.title_button.isHidden())
        self.assertFalse(n.history_button.isHidden())
        self.assertFalse(n.new_button.isHidden())
        self.assertLessEqual(Q.CGPathGetPathBoundingBox(n.shape.path()).size.width, n.compose_width)
        self.assertEqual([row.chat_id for row in n.history.rows], [first])
        first_row = next(row for row in n.history.rows if row.chat_id == first)
        n.history.selectChat_(first_row)
        self.assertEqual(n.mode, "chat")
        self.assertEqual(n.chat.store.active, first)
        self.assertEqual(n.chat.prompt.string(), "Draft for the first chat")
        self.assertEqual(n.chat.files, [str(self.file)])
        self.assertEqual(n.chat.messages[-1].text, "Partial")
        self.assertIsNotNone(n.chat.pending)
        self.finish_reply(first_request, "First reply")
        self.assertEqual(n.chat.messages[-1].text, "First reply")
        self.assertIsNone(n.chat.pending)
        self.assertTrue(n.chat.select_chat(second))
        self.assertEqual(n.chat.prompt.string(), "Draft for the second chat")
        self.assertEqual(n.chat.messages, [])

    def test_send_submits_history_and_brain_mode_and_streams_agent_events_into_storage(self):
        n = self.notch
        self.assertFalse(n.chat.store.brain)
        self.type("First question")
        n.chat.send_(None)
        first = self.requests[-1]
        self.assertEqual((first.text, first.history, first.brain), ("First question", (), False))
        self.assertFalse(n.chat.send_button.isEnabled())
        self.finish_reply(text="", status="failed", error="Luna couldn't finish that reply. Try again.")
        # The failure sits in place of the reply, with Try again, not in a status line.
        failed = n.chat.messages[-1]
        self.assertEqual(failed.text, "")
        self.assertEqual(failed.error, "Luna couldn't finish that reply. Try again.")
        self.assertFalse(failed.retry_button.isHidden())
        self.assertEqual(n.chat.status, "")
        self.assertEqual(n.chat.think_button.accessibilityValue(), "off")
        n.chat.think_button.performClick_(None)
        self.assertTrue(n.chat.store.brain)
        self.assertEqual(n.chat.think_button.accessibilityValue(), "on")
        self.type("Second question")
        n.chat.send_(None)
        second = self.requests[-1]
        self.assertTrue(second.brain)
        # Failed replies are not sent back to the model as Luna's words.
        self.assertEqual([(t["role"], t["text"]) for t in second.history], [("user", "First question")])
        steps = (("Calendar", "List events"), ("Calendar", "Update event"))
        n.chat.receive(TextEvent(second.chat_id, second.message_id, "Thinking it", "pending", BRAIN_MODEL,
                                 steps=steps[:1]))
        bubble = n.chat.messages[-1]
        self.assertEqual(bubble.text, "Thinking it")
        self.assertIn("Used Calendar · 1 step", bubble.steps_button.attributedTitle().string())
        self.assertEqual(n.chat.store.messages()[-1]["status"], "pending")
        n.chat.receive(TextEvent(second.chat_id, second.message_id, "Thinking it through.", "complete", BRAIN_MODEL,
                                 steps=steps))
        self.assertEqual(n.chat.status, "")
        stored = n.chat.store.messages()[-1]
        self.assertEqual((stored["text"], stored["status"], stored["model"], stored["steps"]),
                         ("Thinking it through.", "complete", BRAIN_MODEL, list(steps)))
        n.chat.select_chat(n.chat.store.active)
        reloaded = n.chat.messages[-1]
        self.assertEqual(reloaded.steps, steps)
        self.assertIn("Used Calendar · 2 steps", reloaded.steps_button.attributedTitle().string())
        height = reloaded.measure(400)[1]
        reloaded.toggleSteps_(None)
        self.assertGreater(reloaded.measure(400)[1], height)
        self.assertFalse(reloaded.step_labels[1].isHidden())
        self.type("Third")
        n.chat.send_(None)
        self.assertEqual([t["role"] for t in self.requests[-1].history], ["user", "user", "luna"])
        self.finish_reply(text="Done.", model=FAST_MODEL)
        self.assertFalse(n.chat.attention)

    def test_replies_never_show_model_names_or_ids(self):
        n = self.notch
        for model in (FAST_MODEL, BRAIN_MODEL, "gemini-3.8-flash"):
            self.type("Plan my week")
            n.chat.send_(None)
            self.finish_reply(text="Here's a plan.", model=model)
        n.chat.select_chat(n.chat.store.active)
        shown = []
        for bubble in n.chat.messages:
            shown += [view.stringValue() for view in bubble.subviews() if hasattr(view, "stringValue")]
        shown += [n.chat.think_button.toolTip(), n.chat.send_button.accessibilityLabel()]
        for text in shown:
            for word in ("Qwen", "Ollama", "Gemini", "gemini", "qwen", "Flash"):
                self.assertNotIn(word, text)

    def test_steps_are_one_quiet_line_that_expands(self):
        n = self.notch
        self.type("Check my calendar")
        n.chat.send_(None)
        steps = (("Calendar", "List events"), ("Calendar", "Move the weekly sync to Thursday"))
        self.finish_reply(text="Moved.", steps=steps)
        bubble = n.chat.messages[-1]
        self.assertIn("Used Calendar · 2 steps", bubble.steps_button.attributedTitle().string())
        closed = bubble.measure(400)[1]
        self.assertTrue(all(item.isHidden() for item in bubble.step_labels))
        bubble.toggleSteps_(None)
        self.assertGreater(bubble.measure(400)[1], closed)
        self.assertFalse(bubble.step_labels[1].isHidden())

    def test_try_again_reuses_the_failed_reply_without_duplicating_the_question(self):
        n = self.notch
        self.type("Compare flights to Lisbon")
        n.chat.send_(None)
        first = self.requests[-1]
        self.finish_reply(text="", status="failed", error="Luna couldn't finish that reply.")
        self.assertTrue(n.chat.attention)
        n.chat.messages[-1].retry_(None)
        again = self.requests[-1]
        self.assertEqual((again.message_id, again.text, again.history), (first.message_id, first.text, ()))
        self.assertIsNotNone(n.chat.pending)
        self.assertIsNone(n.chat.messages[-1].error)
        self.assertEqual([m["role"] for m in n.chat.store.messages()], ["user", "luna"])
        self.assertEqual(n.chat.store.messages()[-1]["status"], "pending")
        self.finish_reply(again, "Flights on the 14th are shortest.")
        self.assertFalse(n.chat.attention)

    def test_hover_starts_compact_and_only_sending_or_picking_a_chat_opens_the_full_window(self):
        n = self.notch
        self.type("Plan my afternoon")
        n.chat.send_(None)
        self.finish_reply()
        n._draw(animate=False)
        self.assertEqual(n.mode, "chat")
        self.assertEqual(n.chat.placeholder.stringValue(), "Reply to Luna")
        n.hovered = False
        n._settle()
        n._draw(animate=False)
        n.hovered = True
        n._draw(animate=False)
        self.assertEqual(n.mode, "compose")
        self.assertLessEqual(Q.CGPathGetPathBoundingBox(n.shape.path()).size.width, n.compose_width)
        self.assertEqual(n.chat.placeholder.stringValue(), "Ask Luna anything")
        self.assertFalse(n.history_button.isHidden())
        self.assertTrue(n.title_button.isHidden())
        n.newChat_(None)
        self.assertEqual(n.mode, "compose")

    def test_typeahead_shows_last_three_then_completes_and_searches_messages(self):
        n = self.notch
        for title, reply in (("Lisbon trip ideas", "Flights on the 14th are shortest."),
                             ("Invoice from the studio", "The quote covers the Q4 illustrations."),
                             ("Q4 roadmap summary", "Three themes: reliability, dashboard, widgets."),
                             ("Focus playlist", "Playing Deep Focus.")):
            n.newChat_(None)
            self.type(title)
            n.chat.send_(None)
            self.finish_reply(text=reply)
        n.newChat_(None)
        n.showHistory_(None)
        n._draw(animate=False)
        self.assertEqual(n.mode, "history")
        self.assertEqual([r.title_label.stringValue() for r in n.history.rows],
                         ["Focus playlist", "Q4 roadmap summary", "Invoice from the studio"])
        self.assertEqual(n.history.sections, [])
        self.assertTrue(n.history.rows[0].selected)
        n.history.search.setStringValue_("q4")
        n.history.controlTextDidChange_(None)
        self.assertEqual(n.history.completion, " roadmap summary")
        self.assertEqual([s.stringValue() for s in n.history.sections], ["Chat", "Mentioned in"])
        self.assertEqual([r.title_label.stringValue() for r in n.history.rows],
                         ["Q4 roadmap summary", "Invoice from the studio"])
        self.assertIn("Q4 illustrations", n.history.rows[1].snippet.stringValue())
        n.history.move(1)
        self.assertTrue(n.history.rows[1].selected)
        n.history.open_selected()
        self.assertEqual(n.mode, "chat")
        self.assertEqual(n.chat.store.current["title"], "Invoice from the studio")
        n.showHistory_(None)
        n.history.search.setStringValue_("nothing like this")
        n.history.controlTextDidChange_(None)
        self.assertEqual(n.history.rows, [])
        self.assertEqual(n.history.sections[0].stringValue(), "No chats mention “nothing like this”.")
        n.closeHistory_(None)
        self.assertEqual(n.mode, "chat")

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
            self.assertAlmostEqual(n.mark.layer.position().x, header_left + 30)
            self.assertAlmostEqual(n.history_button.frame().origin.x,
                                   n.canvas_width - header_left - 96)
            inset = max(0, n.motion.value[2]) + PAD
            self.assertAlmostEqual(n.chat.view.frame().size.width, max(1, bounds.size.width - 2 * inset))

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
        # Luna's reply has no bubble: its text starts at the top edge and grows downward.
        self.assertEqual(bubble.label.frame().origin.y, 0)


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
