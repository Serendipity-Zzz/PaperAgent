export const MESSAGE_WINDOW_SIZE = 200;
export const LATEST_MESSAGE_CURSOR = 2_147_483_647;

export function boundedMessageWindow<T>(messages: T[]): T[] {
  return messages.length > MESSAGE_WINDOW_SIZE
    ? messages.slice(-MESSAGE_WINDOW_SIZE)
    : messages;
}
