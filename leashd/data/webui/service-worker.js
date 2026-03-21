/* leashd Service Worker — push notifications only (no caching/offline) */
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  if (!event.data) return;

  let data;
  try {
    data = event.data.json();
  } catch {
    data = { title: "leashd", body: event.data.text() };
  }

  const title = data.title || "leashd";
  const actionable = ["approval_request", "question", "interrupt_prompt"].includes(data.event_type);
  const options = {
    body: data.body || "",
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    tag: data.event_type || "leashd",
    renotify: true,
    requireInteraction: actionable,
    vibrate: [200, 100, 200],
    data: { url: data.url || "/" },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const targetUrl = event.notification.data?.url || "/";

  const base = self.registration.scope;
  const fullUrl = new URL(targetUrl, base).href;

  // Reject off-origin URLs to prevent phishing via compromised push payloads
  if (!fullUrl.startsWith(self.location.origin)) return;

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        for (const client of clients) {
          const clientPath = new URL(client.url).pathname;
          if (clientPath === "/") {
            return client.navigate(fullUrl).then((c) => c && c.focus());
          }
        }
        return self.clients.openWindow(fullUrl);
      })
  );
});
