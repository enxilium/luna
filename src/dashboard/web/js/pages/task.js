// A task that belongs to nothing, opened on its own page. Tasks in a project or course open inside it instead.

import { h, api, go } from "../core.js";
import { empty } from "../ui.js";
import { taskDoc, ownerChip, preloadEditor } from "../work.js";

export default {
  title: "Task",
  events: ["tasks"],
  cached: (params) => api.cached(`/api/tasks/${params[0]}`) || null,
  async load(params) {
    const task = await api.get(`/api/tasks/${params[0]}`);
    // Moved into a project or course since the link was made: open it there.
    if (task.owner) { go(task.link); return task; }
    preloadEditor();
    return task;
  },
  render(task) {
    if (!task || !task.id) return empty("checklist", "That task is gone", null);
    const chip = ownerChip({ label: "Home", hue: "iris", icon: "house" }, { onClick: () => go("home"), title: "Back to Home" });
    return h("div", { class: "work is-solo" }, taskDoc(task, { chip, onDeleted: () => go("home") }));
  },
};
