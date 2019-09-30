(* Copyright (c) 2016-present, Facebook, Inc.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree. *)

open Core
open OUnit2
open Ast
open Analysis
open Pyre
open Test

let ignore_define_location { Annotation.annotation; mutability } =
  let ignore annotation =
    match annotation with
    | Type.Callable ({ implementation; overloads; _ } as callable) ->
        let callable =
          let remove callable = { callable with Type.Callable.define_location = None } in
          {
            callable with
            implementation = remove implementation;
            overloads = List.map overloads ~f:remove;
          }
        in
        Type.Callable callable
    | _ -> annotation
  in
  let annotation = ignore annotation in
  let mutability =
    match mutability with
    | Mutable -> Annotation.Mutable
    | Immutable immutable -> Immutable { immutable with original = ignore immutable.original }
  in
  { Annotation.annotation; mutability }


let test_simple_registration context =
  let assert_registers source name ?original expected =
    let project = ScratchProject.setup ["test.py", source] ~context in
    let ast_environment, ast_environment_update_result = ScratchProject.parse_sources project in
    let class_metadata_environment, update_result =
      update_environments
        ~ast_environment:(AstEnvironment.read_only ast_environment)
        ~configuration:(ScratchProject.configuration_of project)
        ~ast_environment_update_result
        ~qualifiers:(Reference.Set.singleton (Reference.create "test"))
        ()
    in
    let annotated_global_environment =
      AnnotatedGlobalEnvironment.create
        (ClassMetadataEnvironment.read_only class_metadata_environment)
    in
    let _ =
      AnnotatedGlobalEnvironment.update
        annotated_global_environment
        ~scheduler:(mock_scheduler ())
        ~configuration:(Configuration.Analysis.create ())
        update_result
    in
    let read_only = AnnotatedGlobalEnvironment.read_only annotated_global_environment in
    let printer global =
      global
      >>| GlobalResolution.sexp_of_global
      >>| Sexp.to_string_hum
      |> Option.value ~default:"None"
    in
    let location_insensitive_compare left right =
      Option.compare GlobalResolution.compare_global left right = 0
    in
    assert_equal
      ~cmp:location_insensitive_compare
      ~printer
      ( expected
      >>| Annotation.create_immutable ~global:true ?original
      >>| Node.create_with_default_location )
      ( AnnotatedGlobalEnvironment.ReadOnly.get_global read_only (Reference.create name)
      >>| Node.map ~f:ignore_define_location )
  in
  assert_registers "x = 1" "test.x" (Some Type.integer);
  assert_registers "x, y, z  = 'A', True, 1.8" "test.x" (Some Type.string);
  assert_registers "x, y, z  = 'A', True, 1.8" "test.z" (Some Type.float);

  (* Tuple assignment is all or nothing *)
  assert_registers "x, y  = 'A', True, 1.8" "test.x" (Some Type.Top);
  assert_registers
    {|
      class P: pass
    |}
    "test.P"
    (Some (Type.meta (Type.Primitive "test.P")));
  assert_registers
    {|
      class P: pass
      class R: pass
      def foo(x: P) -> R:
       ...
    |}
    "test.foo"
    (Some
       (Type.Callable.create
          ~name:(Reference.create "test.foo")
          ~parameters:
            (Defined
               [
                 Named
                   { annotation = Type.Primitive "test.P"; default = false; name = "$parameter$x" };
               ])
          ~annotation:(Type.Primitive "test.R")
          ()));
  ()


let test_updates context =
  let assert_updates
      ?original_source
      ?new_source
      ~middle_actions
      ~expected_triggers
      ?post_actions
      ()
    =
    Memory.reset_shared_memory ();
    let sources = original_source >>| (fun source -> "test.py", source) |> Option.to_list in
    let project =
      ScratchProject.setup
        ~include_typeshed_stubs:false
        ~incremental_style:FineGrained
        sources
        ~context
    in
    let ast_environment, ast_environment_update_result = ScratchProject.parse_sources project in
    let class_metadata_environment, update_result =
      update_environments
        ~ast_environment:(AstEnvironment.read_only ast_environment)
        ~configuration:(ScratchProject.configuration_of project)
        ~ast_environment_update_result
        ~qualifiers:(Reference.Set.singleton (Reference.create "test"))
        ()
    in
    let annotated_global_environment =
      AnnotatedGlobalEnvironment.create
        (ClassMetadataEnvironment.read_only class_metadata_environment)
    in
    let configuration = ScratchProject.configuration_of project in
    let _ =
      AnnotatedGlobalEnvironment.update
        annotated_global_environment
        ~scheduler:(mock_scheduler ())
        ~configuration
        update_result
    in
    let read_only = AnnotatedGlobalEnvironment.read_only annotated_global_environment in
    let execute_action = function
      | global_name, dependency, expectation ->
          let location_insensitive_compare left right =
            Option.compare GlobalResolution.compare_global left right = 0
          in
          let printer global =
            global
            >>| GlobalResolution.sexp_of_global
            >>| Sexp.to_string_hum
            |> Option.value ~default:"None"
          in
          let expectation =
            expectation
            >>| Annotation.create_immutable ~global:true
            >>| Node.create_with_default_location
          in
          AnnotatedGlobalEnvironment.ReadOnly.get_global
            read_only
            (Reference.create global_name)
            ~dependency
          >>| Node.map ~f:ignore_define_location
          |> assert_equal ~cmp:location_insensitive_compare ~printer expectation
    in
    List.iter middle_actions ~f:execute_action;
    let add_file
        { ScratchProject.configuration = { Configuration.Analysis.local_root; _ }; _ }
        content
        ~relative
      =
      let content = trim_extra_indentation content in
      let file = File.create ~content (Path.create_relative ~root:local_root ~relative) in
      File.write file
    in
    let delete_file
        { ScratchProject.configuration = { Configuration.Analysis.local_root; _ }; _ }
        relative
      =
      Path.create_relative ~root:local_root ~relative |> Path.absolute |> Core.Unix.remove
    in
    if Option.is_some original_source then
      delete_file project "test.py";
    new_source >>| add_file project ~relative:"test.py" |> Option.value ~default:();
    let { ScratchProject.module_tracker; _ } = project in
    let { Configuration.Analysis.local_root; _ } = configuration in
    let path = Path.create_relative ~root:local_root ~relative:"test.py" in
    let update_result =
      let ast_environment_update_result =
        ModuleTracker.update ~configuration ~paths:[path] module_tracker
        |> (fun updates -> AstEnvironment.Update updates)
        |> AstEnvironment.update ~configuration ~scheduler:(mock_scheduler ()) ast_environment
      in
      update_environments
        ~ast_environment:(AstEnvironment.read_only ast_environment)
        ~configuration:(ScratchProject.configuration_of project)
        ~ast_environment_update_result
        ~qualifiers:(Reference.Set.singleton (Reference.create "test"))
        ()
      |> snd
      |> AnnotatedGlobalEnvironment.update
           annotated_global_environment
           ~scheduler:(mock_scheduler ())
           ~configuration:(ScratchProject.configuration_of project)
    in
    let printer set =
      SharedMemoryKeys.DependencyKey.KeySet.elements set
      |> List.to_string ~f:SharedMemoryKeys.show_dependency
    in
    let expected_triggers = SharedMemoryKeys.DependencyKey.KeySet.of_list expected_triggers in
    assert_equal
      ~printer
      expected_triggers
      (AnnotatedGlobalEnvironment.UpdateResult.triggered_dependencies update_result);
    post_actions >>| List.iter ~f:execute_action |> Option.value ~default:()
  in
  let dependency = SharedMemoryKeys.TypeCheckSource (Reference.create "dep") in
  assert_updates
    ~original_source:{|
      x = 7
    |}
    ~new_source:{|
      y = 9
    |}
    ~middle_actions:["test.x", dependency, Some Type.integer]
    ~expected_triggers:[dependency]
    ~post_actions:["test.x", dependency, None]
    ();
  assert_updates
    ~original_source:{|
      x = 7
    |}
    ~new_source:{|
      x = 9
    |}
    ~middle_actions:["test.x", dependency, Some Type.integer]
    ~expected_triggers:[]
    ~post_actions:["test.x", dependency, Some Type.integer]
    ();
  assert_updates
    ~original_source:{|
      x = 7
    |}
    ~new_source:{|
      x, y = 7, 8
    |}
    ~middle_actions:["test.x", dependency, Some Type.integer]
    ~expected_triggers:[]
    ~post_actions:["test.x", dependency, Some Type.integer]
    ();
  ()


let () =
  "environment"
  >::: ["simple_registration" >:: test_simple_registration; "updates" >:: test_updates]
  |> Test.run
