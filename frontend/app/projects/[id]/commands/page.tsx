import CommandPanel from "./command-panel";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  return <CommandPanel projectId={(await params).id} />;
}
