import ArtifactPanel from './artifact-panel';
export default async function Page({params}:{params:Promise<{id:string}>}){return <ArtifactPanel projectId={(await params).id}/>;}
