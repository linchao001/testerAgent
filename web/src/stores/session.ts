/** 当前工作区 / 会话 / 任务选择（dd §12.1；Zustand）。 */

import { create } from 'zustand'

export type SessionState = {
  workspaceId: string | null
  conversationId: string | null
  taskId: string | null
  setWorkspaceId: (id: string | null) => void
  setConversationId: (id: string | null) => void
  setTaskId: (id: string | null) => void
  reset: () => void
}

export const useSession = create<SessionState>((set) => ({
  workspaceId: null,
  conversationId: null,
  taskId: null,
  setWorkspaceId: (workspaceId) => set({ workspaceId }),
  setConversationId: (conversationId) => set({ conversationId }),
  setTaskId: (taskId) => set({ taskId }),
  reset: () =>
    set({ workspaceId: null, conversationId: null, taskId: null }),
}))
