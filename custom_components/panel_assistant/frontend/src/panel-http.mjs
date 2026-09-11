// The ADB service that reaches the app's local HTTP port on the panel.
// The trailing NUL is part of the contract. Host adb sends service names
// NUL-terminated, and some panels' adbd parse "tcp:<port>" by reading a C
// string past the end of the packet. Without the terminator that read picks up
// trailing bytes, the port no longer parses, and adbd refuses the connection
// as "arbitrary tcp". Newer adbd strips trailing NULs, so this form works on both.
export const PANEL_HTTP_SERVICE = 'tcp:8888\0';
