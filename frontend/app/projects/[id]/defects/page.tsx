import DefectPanel from './defect-panel';
export default async function Page({params}:{params:Promise<{id:string}>}){return <DefectPanel projectId={(await params).id}/>;}
