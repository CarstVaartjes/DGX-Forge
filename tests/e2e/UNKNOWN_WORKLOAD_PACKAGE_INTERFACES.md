# W18 interface report

The deterministic simulator in `test_unknown_workload_package.py` can exercise
the public control publisher, workload-TUF metadata, agent protocol registry,
and `PackageEngine` with direct native and OCI provider bytes. It intentionally
does not claim that the installed GPU node agent currently has this path.

The remaining production interfaces are:

1. `vonk_agent.main.build_agent()` must construct and attach a generic
   `PackageEngine` to `OperationContext.packages`, including durable package
   state, direct component acquisition, materialization, ABI-v1 adapter
   invocation, preflight, cancellation, and progress callbacks.
2. The installed agent needs a concrete `WorkloadTargetSource` for its
   workload-only TUF cache and control routes. `WorkloadTrust` is currently a
   protocol consumer only; it has no installed transport implementation.
3. The workload data-plane needs a public inference/route observation contract
   bound to the active package generation. Package operations currently cover
   prepare, activate, health, rollback, and maintenance, but no inference
   operation or route publication acknowledgement.

The agent-side assembly also needs three concrete adapters before an installed
runtime can be truthful: protocol-lock components must be converted to the
distinct acquisition-provider descriptor type; OCI digest references need a
real immutable OCI acquisition implementation; and the materialized signed
adapter must be bound to `AdapterExecutor`. None of those adapters exists
today. Reusing the platform TUF root, or treating `WorkloadTrustDelivery` as a
consumer verifier, would merge trust domains or bypass signature verification.

Until those interfaces exist, the strict XFAIL in the acceptance test prevents
the simulator from being treated as physical installed-agent acceptance.
