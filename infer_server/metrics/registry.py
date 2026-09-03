# metrics/registry.py
class MonitorRegistry:
    def __init__(self):
        self._registry = {}

    def register(self, name, instance):
        self._registry[name] = instance

    def get(self, name):
        return self._registry.get(name)

    def start_all(self):
        for instance in self._registry.values():
            instance.start()

    @property
    def rps_monitor(self):
        return self._registry.get("rps_monitor")

    @property
    def queue_monitor(self):
        return self._registry.get("queue_monitor")

    @property
    def prometheus_exporter(self):
        return self._registry.get("prometheus")

    @property
    def completion_monitor(self):
        return self._registry.get("completion")

# metrics/registry.py（繼續）
monitorRegistry = MonitorRegistry()
