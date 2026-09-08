const MAX_RESPONSE_BYTES = 4096;

export async function readBounded(stream, maximum = MAX_RESPONSE_BYTES) {
  if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 32768) throw new Error('malformed');
  const reader = stream.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
  let bytes = 0;
  let text = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return text + decoder.decode();
      bytes += value.byteLength;
      if (bytes > maximum) throw new Error('malformed');
      text += decoder.decode(value, { stream: true });
    }
  } finally {
    // Cancelling the reader also ends oversized responses without buffering them.
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
