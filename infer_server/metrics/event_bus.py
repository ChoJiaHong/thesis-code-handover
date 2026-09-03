class EventBus:
    def __init__(self):
        self._subscribers = {}
        self._monitors = set()

    def subscribe(self, event_name: str, monitor):
        self._subscribers.setdefault(event_name, []).append(monitor)
        self._monitors.add(monitor)

    def emit(self, event_name: str, **data):
        for monitor in self._subscribers.get(event_name, []):
            handler = getattr(monitor, 'handle_event', None)
            if callable(handler):
                handler(event_name, **data)

    def start_all(self):
        for monitor in list(self._monitors):
            start = getattr(monitor, 'start', None)
            if callable(start):
                start()


event_bus = EventBus()
