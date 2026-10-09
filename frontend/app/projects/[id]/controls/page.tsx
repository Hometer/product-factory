import ControlsPanel from "./controls-panel";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  return <ControlsPanel projectId={(await params).id} />;
}
