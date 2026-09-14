// LIFECYCLE: permanent
// Resource ownership for the existing release-cloud-probe execution lane.
import { digest } from './resource-input.mjs';

export async function createProbeResources(adapter, candidate, probe, prefix, journal, persist) {
  const resources = candidate.resource_plan.resources.filter(row => ['r2', 'queue', 'vectorize'].includes(row.kind));
  for (const original of resources) {
    const resource = { ...original, name: `${prefix}-${original.key.replace(':', '-')}` };
    if (digest(await adapter.observeResource(resource)) !== digest({ status: 'absent' }))
      throw new Error('private resource name is already occupied');
    const event = { resource, original_name: original.name, state: 'in_flight' };
    journal.resources.push(event);
    persist();
    const created = await adapter.create(resource);
    if (typeof created.created_id !== 'string' || !/^[a-zA-Z0-9-]{1,100}$/.test(created.created_id))
      throw new Error('private resource creation omitted its identity');
    event.created_id = created.created_id;
    persist();
    const observed = await adapter.observeResource(resource);
    if (observed.status !== 'present' || observed.id !== event.created_id)
      throw new Error('private resource creation was not observed');
    event.state = 'created';
    persist();
  }
  probe.resource_plan.resources = probe.resource_plan.resources.map(resource => {
    const event = journal.resources.find(row => row.resource.key === resource.key);
    return event ? event.resource : resource;
  });
  // Install the same metadata indexes and lifecycle policies as the candidate.
  for (const policy of adapter.policies()) {
    if (policy.retain) continue;
    if (adapter.addPolicy(policy).exit !== 0) throw new Error('private resource policy installation failed');
    await adapter.waitForPolicy(policy);
  }
}

export function privateResourceBindings(config, events) {
  const name = (kind, value) => {
    const owned = events.filter(row => row.resource.kind === kind && row.original_name === value && row.state === 'created');
    if (owned.length !== 1) throw new Error(`probe cannot bind to a serving ${kind} resource`);
    return owned[0].resource.name;
  };
  for (const key of ['kv_namespaces', 'hyperdrive', 'analytics_engine_datasets', 'dispatch_namespaces', 'pipelines', 'send_email'])
    if (config[key]?.length) throw new Error(`probe has no isolated resource owner for ${key}`);
  for (const binding of config.r2_buckets ?? []) {
    binding.bucket_name = name('r2', binding.bucket_name);
    delete binding.preview_bucket_name;
  }
  for (const binding of config.vectorize ?? []) binding.index_name = name('vectorize', binding.index_name);
  for (const binding of [...(config.queues?.producers ?? []), ...(config.queues?.consumers ?? [])]) {
    binding.queue = name('queue', binding.queue);
    if (binding.dead_letter_queue) binding.dead_letter_queue = name('queue', binding.dead_letter_queue);
  }
  return config;
}

export async function cleanupProbeResource(adapter, event, { purgeBucket, workerNames }) {
  const observed = await adapter.observeResource(event.resource);
  if (observed.status === 'absent') return;
  if (!event.created_id || observed.status !== 'present' || observed.id !== event.created_id)
    throw new Error('private resource cleanup cannot establish ownership');
  const { kind, name } = event.resource;
  if (kind === 'r2') await purgeBucket(name);
  if (kind === 'queue') {
    const result = (await adapter.api(`queues/${event.created_id}/consumers`)).result;
    if (!Array.isArray(result)) throw new Error('private queue consumer inventory is incomplete');
    for (const consumer of result) {
      if (!workerNames.includes(consumer.script_name) || !/^[a-zA-Z0-9-]+$/.test(consumer.consumer_id ?? ''))
        throw new Error('private queue has an external consumer');
      await adapter.api(`queues/${event.created_id}/consumers/${consumer.consumer_id}`, { method: 'DELETE' });
    }
  }
  const path = { r2: `r2/buckets/${name}`, queue: `queues/${event.created_id}`, vectorize: `vectorize/v2/indexes/${name}` }[kind];
  if (!path) throw new Error('unsupported private resource cleanup');
  await adapter.api(path, { method: 'DELETE' });
  if (digest(await adapter.observeResource(event.resource)) !== digest({ status: 'absent' }))
    throw new Error('private resource deletion is not confirmed');
}
