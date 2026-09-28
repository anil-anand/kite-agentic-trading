import type { BrowserWindow, IpcMainInvokeEvent } from 'electron';
import { sameDocumentLocation } from '../shared/navigation-policy';

export function validateIpcSender(event: IpcMainInvokeEvent, window: BrowserWindow | null, trustedUrl: string): void {
  if (!window || window.isDestroyed() || event.sender !== window.webContents
    || event.senderFrame !== window.webContents.mainFrame
    || !event.senderFrame || !sameDocumentLocation(event.senderFrame.url, trustedUrl)) {
    throw new Error('Untrusted IPC sender');
  }
}
