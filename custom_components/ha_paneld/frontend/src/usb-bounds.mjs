// Reject a peer-supplied ADB payload length before WebUSB allocates its buffer.
export const MAX_USB_TRANSFER = 1024 * 1024;
const bounded = new WeakSet();
export function boundedDevice(device) {
  if (bounded.has(device)) return device;
  const transferIn = device.transferIn.bind(device);
  // Preserve the native USBDevice identity and brand for connection events and
  // the SDK's disconnect equality check. If this object cannot be extended,
  // defineProperty throws before any connection is opened (fail closed).
  Object.defineProperty(device, 'transferIn', {
    value(endpoint, length) {
      if (!Number.isSafeInteger(length) || length < 0 || length > MAX_USB_TRANSFER) {
        return Promise.reject(new Error('malformed'));
      }
      return transferIn(endpoint, length);
    },
  });
  bounded.add(device);
  return device;
}

export function boundedUsb(usb) {
  return new Proxy(usb, {
    get(target, key) {
      if (key === 'requestDevice') return async options => boundedDevice(await target.requestDevice(options));
      if (key === 'getDevices') return async () => (await target.getDevices()).map(boundedDevice);
      const value = Reflect.get(target, key, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}
