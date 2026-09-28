import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { ChatPage } from './pages/ChatPage'
import { SessionPage } from './pages/SessionPage'
import { F0DevPage } from './pages/F0DevPage'
import { RetrievalDebugPage } from './pages/RetrievalDebugPage'
import { SettingsPage } from './pages/SettingsPage'
import { StageConfirmPage } from './pages/StageConfirmPage'
import { WorkbenchPage } from './pages/WorkbenchPage'
import { WorkspacesPage } from './pages/WorkspacesPage'

const isDev = import.meta.env.DEV

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      {
        index: true,
        element: <SessionPage />,
      },
      {
        path: 'session',
        element: <SessionPage />,
      },
      {
        path: 'chat',
        element: <ChatPage />,
      },
      {
        path: 'confirm',
        element: <StageConfirmPage />,
      },
      {
        path: 'workbench',
        element: <WorkbenchPage />,
      },
      {
        path: 'debug',
        element: <RetrievalDebugPage />,
      },
      {
        path: 'workspaces',
        element: <WorkspacesPage />,
      },
      {
        path: 'settings',
        element: <SettingsPage />,
      },
      ...(isDev
        ? [
            {
              path: '_dev/f0',
              element: <F0DevPage />,
            },
          ]
        : []),
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
