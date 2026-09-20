// CGVTItem layout and these exports are bound to the fingerprinted Global.dll.
// Strings use hex-encoded GBK at the process boundary; no C++ objects cross runtimes.
static void symbol_hex(const char* value){
  if(!value||!*value){printf("-");return;}
  for(const unsigned char* p=(const unsigned char*)value;*p;p++)printf("%02x",*p);
}
static int unhex_symbol(const char* text,char* out,size_t capacity){
  if(strcmp(text,"-")==0){out[0]=0;return 1;}
  size_t length=strlen(text);if(length%2||length/2>=capacity)return 0;
  for(size_t i=0;i<length;i+=2){
    unsigned int v=0;char pair[3]={text[i],text[i+1],0};
    if(strspn(pair,"0123456789abcdefABCDEF")!=2||sscanf_s(pair,"%x",&v)!=1||!v)return 0;
    out[i/2]=(char)v;
  }
  out[length/2]=0;return 1;
}
static int symbols_command(HMODULE global,void* center,const char* file,const char* request){
  typedef void* (__thiscall *GetPtr)(void*);
  typedef int (__thiscall *GetSize)(void*);
  typedef void* (__thiscall *GetAt)(void*,int);
  auto get=(GetPtr)GetProcAddress(global,"?GetGVTList@CDataManageCenter@@QAEPAVCGVTList@@XZ");
  auto size=(GetSize)GetProcAddress(global,"?GetSize@CGVTList@@QBEHXZ");
  auto at=(GetAt)GetProcAddress(global,"?GetAt@CGVTList@@QBEPAVCGVTItem@@H@Z");
  if(!get||!size||!at)return 24;
  void* list=get(center);int count=size(list);
  if(count<0||count>100000)return 25;
  if(request){
    FILE* input=0;if(fopen_s(&input,request,"rb")||!input)return 26;
    char line[10000];int changes=0;
    while(fgets(line,sizeof(line),input)){
      line[strcspn(line,"\r\n")]=0;
      char* context=0;char* index=strtok_s(line,"\t",&context);
      char* name=strtok_s(0,"\t",&context);char* address=strtok_s(0,"\t",&context);char* comment=strtok_s(0,"\t",&context);
      char n[257],a[65],c[4097];char* end=0;
      long row_index=index?strtol(index,&end,10):-2;
      if(!index||*end||!name||!address||!comment||strtok_s(0,"\t",&context)||row_index < -1||row_index>=size(list)
         ||!unhex_symbol(name,n,sizeof(n))||!unhex_symbol(address,a,sizeof(a))||!unhex_symbol(comment,c,sizeof(c))){fclose(input);return 27;}
      void* row=0;
      if(row_index==-1){
        typedef void* (__stdcall *Create)();
        row=((Create)GetProcAddress(global,"?CreateObject@CGVTItem@@SGPAVCObject@@XZ"))();
      }else row=at(list,(int)row_index);
      if(!row){fclose(input);return 28;}
      ((SetPath)GetProcAddress(global,"?SetName@CGVTItem@@QAEXPBD@Z"))(row,n);
      ((SetPath)GetProcAddress(global,"?SetAddress@CGVTItem@@QAEXPBD@Z"))(row,a);
      ((SetPath)GetProcAddress(global,"?SetComment@CGVTItem@@QAEXPBD@Z"))(row,c);
      if(row_index==-1){
        typedef int (__thiscall *Add)(void*,void*);
        ((Add)GetProcAddress(global,"?Add@CGVTList@@QAEHPAVCGVTItem@@@Z"))(list,row);
      }
      changes++;
    }
    fclose(input);if(!changes)return 29;
    ((Dtor)GetProcAddress(global,"?RefreshMap@CGVTList@@QAEXXZ"))(list);
    typedef int (__thiscall *Save)(void*,const char*);
    int saved=((Save)GetProcAddress(global,"?SaveGVTTable@CDataManageCenter@@QAEHPBD@Z"))(center,file);
    if(!saved)return 30;
    printf("symbols_saved=%d\n",changes);
  }
  count=size(list);printf("symbols_count=%d\n",count);
  for(int i=0;i<count;i++){
    void* row=at(list,i);
    printf("symbol\t%d\t",i);symbol_hex(*(char**)((BYTE*)row+4));printf("\t");
    symbol_hex(*(char**)((BYTE*)row+8));printf("\t");symbol_hex(*(char**)((BYTE*)row+12));
    printf("\t%lu\n",*(DWORD*)((BYTE*)row+16));
  }
  printf("symbols_finished=1\n");fflush(stdout);return 0;
}
