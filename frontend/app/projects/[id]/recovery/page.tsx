import RecoveryPanel from "./recovery-panel";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  return <RecoveryPanel projectId={(await params).id} />;
}
