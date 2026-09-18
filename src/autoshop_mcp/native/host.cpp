#include <windows.h>
#include <stdio.h>
#include <objbase.h>
#include <bcrypt.h>
#pragma comment(lib,"bcrypt.lib")
#include "profile.h"
#pragma comment(lib,"ole32.lib")
#pragma comment(lib,"user32.lib")
typedef void* (__thiscall *Ctor)(void*);
typedef void (__thiscall *Dtor)(void*);
typedef void (__thiscall *SetPath)(void*,const char*);
typedef int (__thiscall *LoadAll)(void*,const char*,const char*,const char*,const char*,const char*,int);
bool compileDone=false;
int nativeErrors=0;
HMODULE mfcRuntime=0;
int __cdecl is_encrypted(DWORD value){
  // Internal host only: the Python wrapper rejects protected projects before invoking this callback.
  ((Dtor)GetProcAddress(mfcRuntime,MAKEINTRESOURCEA(601)))(&value);return 0;
}
LRESULT CALLBACK sink_proc(HWND h,UINT m,WPARAM w,LPARAM l){
  if(m==0x52c&&l){
    DWORD* p=(DWORD*)l;if(p[6]==1)nativeErrors++;printf("diagnostic op=%lu level=%lu page=%lu text=%s\n",w,p[6],p[7],p[5]?(char*)p[5]:"");fflush(stdout);return 1;
  }
  if(m==0x465){compileDone=true;printf("convert_finished w=%lu l=%ld\n",w,l);fflush(stdout);return 1;}
  if(m==0x464){compileDone=true;if(l)nativeErrors++;printf("compile_finished w=%lu l=%ld\n",w,l);fflush(stdout);return 1;}
  if(m>=0x400){printf("sink_message=%x w=%lx l=%lx\n",m,w,l);fflush(stdout);}
  return DefWindowProcA(h,m,w,l);
}
int probe_exception_filter(EXCEPTION_POINTERS* e){
  char name[MAX_PATH]={0};HMODULE mod=0;
  GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,(LPCSTR)e->ExceptionRecord->ExceptionAddress,&mod);
  GetModuleFileNameA(mod,name,MAX_PATH);
  printf("exception_at=%s+%08lx code=%08lx target=%08lx ecx=%08lx\n",name,(DWORD)e->ExceptionRecord->ExceptionAddress-(DWORD)mod,e->ExceptionRecord->ExceptionCode,e->ExceptionRecord->ExceptionInformation[1],e->ContextRecord->Ecx);
  DWORD* stack=(DWORD*)e->ContextRecord->Esp;
  for(int i=0;i<32;i++){HMODULE sm=0;char sn[MAX_PATH]={0};if(GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,(LPCSTR)stack[i],&sm)){GetModuleFileNameA(sm,sn,MAX_PATH);printf("stack[%d]=%s+%08lx\n",i,sn,stack[i]-(DWORD)sm);}}
  if(e->ExceptionRecord->ExceptionCode==0xe06d7363){
    DWORD* ti=(DWORD*)e->ExceptionRecord->ExceptionInformation[2];DWORD* ca=(DWORD*)ti[3];DWORD* ct=(DWORD*)ca[1];
    printf("cpp_type=%s thrown=%p\n",(char*)ct[1]+8,(void*)e->ExceptionRecord->ExceptionInformation[1]);
    DWORD* ob=*(DWORD**)e->ExceptionRecord->ExceptionInformation[1];
    printf("cpp_data=%08lx %08lx %08lx %08lx %08lx\n",ob[0],ob[1],ob[2],ob[3],ob[4]);
    if(strstr((char*)ct[1]+8,"CFileException"))printf("exception_file=%s\n",(char*)ob[4]);
  }
  return EXCEPTION_EXECUTE_HANDLER;
}
LONG WINAPI unhandled(EXCEPTION_POINTERS* e){probe_exception_filter(e);fflush(stdout);return EXCEPTION_EXECUTE_HANDLER;}

bool fingerprint(const char* path,const char* expected){
 HANDLE f=CreateFileA(path,GENERIC_READ,FILE_SHARE_READ,0,OPEN_EXISTING,0,0);
 if(f==INVALID_HANDLE_VALUE)return false;
 BCRYPT_ALG_HANDLE a=0;BCRYPT_HASH_HANDLE h=0;unsigned char digest[32],buffer[65536];DWORD got=0;bool ok=false;
 if(BCryptOpenAlgorithmProvider(&a,BCRYPT_SHA256_ALGORITHM,0,0)<0)goto done;
 if(BCryptCreateHash(a,&h,0,0,0,0,0)<0)goto done;
 while(ReadFile(f,buffer,sizeof(buffer),&got,0)&&got)if(BCryptHashData(h,buffer,got,0)<0)goto done;
 if(GetLastError()!=ERROR_SUCCESS && got)goto done;
 if(BCryptFinishHash(h,digest,32,0)<0)goto done;
 {char hex[65];for(int i=0;i<32;i++)sprintf_s(hex+i*2,65-i*2,"%02x",digest[i]);ok=strcmp(hex,expected)==0;}
 done:if(h)BCryptDestroyHash(h);if(a)BCryptCloseAlgorithmProvider(a,0);CloseHandle(f);return ok;
}
int main(int argc,char**argv) {
  SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
  CoInitializeEx(0,COINIT_APARTMENTTHREADED);
  SetUnhandledExceptionFilter(unhandled);
  if(argc!=6&&argc!=7)return 2;
  bool convert=argc==7;
  char library[MAX_PATH];
  for(int i=0;i<4;i++){sprintf_s(library,"%s\\%s",argv[3],profileFiles[i]);if(!fingerprint(library,profileHashes[i])){printf("version_rejected=%s\n",profileFiles[i]);return 20;}}
  ACTCTXA ac={sizeof(ac)}; ac.lpSource=argv[1];
  HANDLE h=CreateActCtxA(&ac);ULONG_PTR cookie=0;
  if(h==INVALID_HANDLE_VALUE||!ActivateActCtx(h,&cookie)){printf("activation_failed %lu\n",GetLastError());return 3;}
  SetDllDirectoryA(argv[3]);
  HMODULE mfc=LoadLibraryA("mfc90.dll");
  mfcRuntime=mfc;
  if(mfc){GetModuleFileNameA(mfc,library,MAX_PATH);if(!fingerprint(library,profileMfcHash)){printf("mfc_version_rejected\n");return 20;}}
  if(!mfc){printf("mfc_failed=%lu\n",GetLastError());return 8;}
  // CWinApp runtime class: size 0xa4. Ordinal 589 ctor verified on this installed MFC90.
  auto appctor=(SetPath)GetProcAddress(mfc,MAKEINTRESOURCEA(589));
  void* app=VirtualAlloc(0,0xa4,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
  appctor(app,"AutoShop native probe");
  printf("mfc_app_constructed=1\n");fflush(stdout);
  sprintf_s(library,"%s\\Global.dll",argv[3]);HMODULE g=LoadLibraryA(library);
  printf("global_loaded=%d error=%lu\n",g!=0,GetLastError());fflush(stdout);
  if(!g)return 4;
  void** encryptedCallback=(void**)GetProcAddress(g,"?m_pGetEncrypted@CBaseData@@2P6AHV?$CStringT@DV?$StrTraitMFC_DLL@DV?$ChTraitsCRT@D@ATL@@@@@ATL@@@ZA");
  *encryptedCallback=(void*)is_encrypted;
  sprintf_s(library,"%s\\Converter.dll",argv[3]);HMODULE c=LoadLibraryA(library);
  printf("converter_loaded=%d error=%lu\n",c!=0,GetLastError());fflush(stdout);
  if(!c)return 5;
  auto ctor=(Ctor)GetProcAddress(g,"??0CDataManageCenter@@QAE@XZ");
  auto dtor=(Dtor)GetProcAddress(g,"??1CDataManageCenter@@UAE@XZ");
  auto path=(SetPath)GetProcAddress(g,"?SetProjectPath@CDataManageCenter@@QAEXPBD@Z");
  if(!ctor||!dtor||!path){printf("missing_export\n");return 6;}
  void* obj=VirtualAlloc(0,0x3cee8,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
  __try {
    ctor(obj);printf("center_constructed=1\n");fflush(stdout);
    char projectPath[MAX_PATH];sprintf_s(projectPath,"%s\\%s",argv[2],argv[5]);
    path(obj,projectPath);printf("project_path_set=1\n");fflush(stdout);
    if(argc>3){
      auto load=(LoadAll)GetProcAddress(g,"?LoadAll@CDataManageCenter@@QAEHPBD0000H@Z");
      char files[4][MAX_PATH];const char* names[]={"VarList.gdt","CrossTable.crs","Config.sdt","DeviceTable.dev"};
      for(int i=0;i<4;i++)sprintf_s(files[i],"%s\\%s",argv[2],names[i]);
      int result=load(obj,files[0],files[1],files[2],files[3],"",0);
      printf("load_all_return=%d\n",result);fflush(stdout);
      HMODULE parser=GetModuleHandleA("Parser.dll");
      DWORD maker=0;
      ((Ctor)GetProcAddress(parser,"??0IQueryerCreater@@QAE@XZ"))(&maker);
      typedef void* (__thiscall *ProjectFactory)(void*,const char*);
      auto factory=(ProjectFactory)GetProcAddress(parser,"?CreateProjectQueryer@IQueryerCreater@@QAEPAVIProjectQueryer@@PBD@Z");

      void* project=factory(&maker,argv[4]);
      printf("project_queryer_created=%d\n",project!=0);fflush(stdout);
      if(!project)return 9;
      DWORD list[7]={0};
      typedef void (__thiscall *ListCtor)(void*,int);
      ((ListCtor)((BYTE*)c+0x6550))(list,10);
      typedef int (__thiscall *GetFiles)(void*,void*,const char*);
      char xpath[100]="/project/file";if(convert)sprintf_s(xpath,"/project/file[@id='%d']",atoi(argv[6]));
      int got=((GetFiles)(*(void***)project)[0])(project,list,xpath);
      if(!got || (convert&&list[3]!=1))return 21;
      printf("project_file_list_return=%d file_count=%lu\n",got,list[3]);fflush(stdout);
      DWORD hwname=0; void* hw=0;void* ins=0;
      char hardwarePath[MAX_PATH];sprintf_s(hardwarePath,"%s\\device\\H3U.dll",argv[3]);
      ((SetPath)GetProcAddress(mfc,MAKEINTRESOURCEA(310)))(&hwname,hardwarePath);
      typedef int (__thiscall *GcmFactory)(void*,DWORD,void**,void**);
      auto gcm=(GcmFactory)GetProcAddress(parser,"?CreateGCMQueryer@IQueryerCreater@@QAEHV?$CStringT@DV?$StrTraitMFC_DLL@DV?$ChTraitsCRT@D@ATL@@@@@ATL@@AAPAVIHardwareQueryer@@AAPAVIInstructionQueryer@@@Z");
      int gcmResult=gcm(&maker,hwname,&hw,&ins);
      printf("gcm_return=%d hw=%p ins=%p\n",gcmResult,hw,ins);fflush(stdout);
      if(!gcmResult||!hw||!ins)return 10;
      typedef void (__thiscall *SetQuery)(void*,void*,void*,void*);
      ((SetQuery)GetProcAddress(g,"?SetQueryer@CDataManageCenter@@QAEXPAVIHardwareQueryer@@PAVIInstructionQueryer@@PAVICommLinkedQueryer@@@Z"))(obj,hw,ins,0);
      ((SetPath)GetProcAddress(mfc,MAKEINTRESOURCEA(310)))(&hwname,hardwarePath);
      typedef void (__thiscall *SetString)(void*,DWORD*);
      ((SetString)GetProcAddress(g,"?SetHardwareFile@CDataManageCenter@@QAEXABV?$CStringT@DV?$StrTraitMFC_DLL@DV?$ChTraitsCRT@D@ATL@@@@@ATL@@@Z"))(obj,&hwname);
      ((Dtor)GetProcAddress(mfc,MAKEINTRESOURCEA(601)))(&hwname);
      typedef void* (__thiscall *CompilerFactory)(void*,void*,void*,int,void*,int);
      char compilerMaker=0;
      auto compileFactory=(CompilerFactory)GetProcAddress(c,"?CreateCompiler@ICompilerCreater@@QAEPAVICompileExecuter@@PAV?$CList@UtagFileProp@@AAU1@@@PAVCWnd@@W4EnumCmpOption@@PAVCDataManageCenter@@H@Z");
      WNDCLASSA wc={0};wc.lpfnWndProc=sink_proc;wc.hInstance=GetModuleHandleA(0);wc.lpszClassName="AutoShopNativeSink";
      RegisterClassA(&wc);HWND sink=CreateWindowExA(0,wc.lpszClassName,"",0,0,0,0,0,HWND_MESSAGE,0,wc.hInstance,0);
      typedef void* (__stdcall *MakeWnd)();
      void* wnd=((MakeWnd)((BYTE*)mfc+0x5f296))();
      *(HWND*)((BYTE*)wnd+0x20)=sink;
      typedef void (__cdecl *SetOutput)(HWND*);
      ((SetOutput)GetProcAddress(g,"?SetOutputWnd@CErrorCenter@@SAXAAPAUHWND__@@@Z"))(&sink);
      ((Dtor)GetProcAddress(g,"?SetStepInfoValid@CDataManageCenter@@QAEXXZ"))(obj);
      typedef void* (__thiscall *ConvertFactory)(void*,void*,void*,int,void*);
      auto convertFactory=(ConvertFactory)GetProcAddress(c,"?CreateConverter@ICompilerCreater@@QAEPAVICompileExecuter@@PAV?$CList@UtagFileProp@@AAU1@@@PAVCWnd@@W4EnumConvertType@@PAVCDataManageCenter@@@Z");
      void* compiler=convert?convertFactory(&compilerMaker,list,wnd,0,obj):compileFactory(&compilerMaker,list,wnd,2,obj,0);
      printf("compiler_created=%d\n",compiler!=0);fflush(stdout);
      if(argc>5){
        SetCurrentDirectoryA(argv[2]);
        char compileDir[MAX_PATH];sprintf_s(compileDir,"%s\\Compile",argv[2]);CreateDirectoryA(compileDir,0);
        if(!convert)((Dtor)((BYTE*)c+0x6cd80))(compiler);
        typedef int (__thiscall *Run)(void*);
        int rc=((Run)(*(void***)compiler)[0])(compiler);
        printf("native_start_return=%d\n",rc);fflush(stdout);
        void* execThread=*(void**)((BYTE*)compiler+(convert?0x3c:0x2c));
        HANDLE threadHandle=0;
        if(!execThread || !DuplicateHandle(GetCurrentProcess(),*(HANDLE*)((BYTE*)execThread+0x2c),GetCurrentProcess(),&threadHandle,SYNCHRONIZE,FALSE,0))return 22;
        DWORD started=GetTickCount();
        while(WaitForSingleObject(threadHandle,0)!=WAIT_OBJECT_0&&GetTickCount()-started<15000){
          MSG message;while(PeekMessageA(&message,0,0,0,PM_REMOVE)){TranslateMessage(&message);DispatchMessageA(&message);}
          MsgWaitForMultipleObjects(threadHandle?1:0,threadHandle?&threadHandle:0,FALSE,25,QS_ALLINPUT);
        }
        MSG finalMsg;while(PeekMessageA(&finalMsg,0,0,0,PM_REMOVE)){TranslateMessage(&finalMsg);DispatchMessageA(&finalMsg);}
        if(!compileDone||WaitForSingleObject(threadHandle,0)!=WAIT_OBJECT_0){printf("compile_timeout\n");return 11;}
        CloseHandle(threadHandle);
      }
      ((Dtor)(*(void***)project)[8])(project);
      typedef void (__thiscall *ScalarDtor)(void*,int);
      ((ScalarDtor)(*(void***)list)[1])(list,0);
    }
    dtor(obj);printf("center_destroyed=1\n");fflush(stdout);
  } __except(probe_exception_filter(GetExceptionInformation())){return 7;}
  return nativeErrors?12:0;
}
