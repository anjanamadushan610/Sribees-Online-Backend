"""
Payments, on the server only.

  catalog.py   every way to pay, with its name, icon and gateway
  gateways/    one adapter per gateway behind one contract (Marx today)
  service.py   an online order's life: awaiting payment, tries, the sweep
  texts.py     what the customer reads about a payment, in en / si / ta
  worker.py    the sweep, every minute

The app draws what these send and holds no payment rules of its own, so a
new method or a new gateway ships with a backend deploy alone.
"""
