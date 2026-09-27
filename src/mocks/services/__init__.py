from mocks.services import customer, order, outbox, payments, refund, replacement, returns

SPECS = {s.name: s for s in (customer.SPEC, order.SPEC, returns.SPEC, refund.SPEC, payments.SPEC,
                             replacement.SPEC, outbox.SPEC)}
