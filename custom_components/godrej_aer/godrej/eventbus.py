import asyncio

class EventBus:
  def __init__(self):
    self.listeners = {}

  def add_listener(self, event_name, listener):
    if not self.listeners.get(event_name, None):
      self.listeners[event_name] = {listener}
    else:
      self.listeners[event_name].add(listener)

  def remove_listener(self, event_name, listener):
    listeners = self.listeners.get(event_name)
    if not listeners:
      return

    listeners.discard(listener)
    if len(listeners) == 0:
      del self.listeners[event_name]

  def send(self, event_name, event_data=None):
    listeners = self.listeners.get(event_name, [])
    for listener in listeners:
      asyncio.create_task(listener(event_data))