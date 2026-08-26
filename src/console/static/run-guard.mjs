export async function startWhenIdle(state, action) {
  if (state.busy) return false;
  await action();
  return true;
}
