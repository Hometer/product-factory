import RequirementEditor from "./requirement-editor";

export default async function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RequirementEditor key={id} projectId={id} />;
}
