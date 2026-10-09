import EvaluationPanel from './evaluation-panel';
export default async function Page({params}:{params:Promise<{id:string}>}) { const {id}=await params; return <EvaluationPanel projectId={id}/>; }
