import VerificationPanel from './verification-panel';
export default async function Page({params}:{params:Promise<{id:string}>}){const {id}=await params;return <VerificationPanel projectId={id}/>;}
