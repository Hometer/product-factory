import PatchEditor from "./patch-editor";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  return <PatchEditor projectId={(await params).id} />;
}
