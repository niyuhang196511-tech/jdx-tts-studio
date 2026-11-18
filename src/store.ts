import { create } from "zustand";

interface UIState {
  projectId: string | null;
  selectedIds: string[];
  selectedClip: string | null;
  tab: "lines" | "video" | "history";
  resource: "projects" | "voices" | "assets";
  theme: "dark" | "light";
  setProject: (id: string | null) => void;
  select: (ids: string[]) => void;
  setClip: (id: string | null) => void;
  setTab: (tab: UIState["tab"]) => void;
  setResource: (resource: UIState["resource"]) => void;
  toggleTheme: () => void;
}
export const useUI = create<UIState>((set) => ({
  projectId: localStorage.getItem("jdx-project"),
  selectedIds: [],
  selectedClip: null,
  tab: "lines",
  resource: "projects",
  theme: localStorage.getItem("jdx-theme") === "light" ? "light" : "dark",
  setProject: (id) => {
    if (id) localStorage.setItem("jdx-project", id);
    else localStorage.removeItem("jdx-project");
    set({ projectId: id, selectedIds: [], selectedClip: null });
  },
  select: (selectedIds) => set({ selectedIds }),
  setClip: (selectedClip) => set({ selectedClip }),
  setTab: (tab) => set({ tab }),
  setResource: (resource) => set({ resource }),
  toggleTheme: () =>
    set((state) => {
      const theme = state.theme === "dark" ? "light" : "dark";
      localStorage.setItem("jdx-theme", theme);
      return { theme };
    }),
}));
