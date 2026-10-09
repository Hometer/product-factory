import AcceptancePanel from './acceptance-panel';
export default async function Page({params}:{params:Promise<{id:string}>}) {return <AcceptancePanel projectId={(await params).id}/>;}
